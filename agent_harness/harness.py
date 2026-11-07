"""harness.py - Harness v1.2 (parts A1 + A2 + A3): the agent loop, the STATE TRACKER and the GUARDRAILS.

  state_tracker.py  SEES  : after every tool call the TRUE state is checked (did it work? anything unexpected? looping?),
                            before every LLM decision: did the scene change by itself?
  guardrails.py     ACTS  : table-edge refusal, false-"done" rejection, fresh look on a scene change / loop,
                            loop / failure / time budgets, LLM retry.

  --panel on     the LLM is also TOLD in words (a "[status]" line after each tool result)       default: off
  --guard on     the guardrails are active                                                         default: off
  --kick-after N FAULT TEST: after the N-th tool call the banana is knocked 20 cm away

    python harness.py --brain rules --seeds 0 26                       # no LLM: checks the harness itself
    python harness.py --seeds 0 1 2                                    # Franky + gemma4:e2b, everything off
    python harness.py --seeds 0 1 2 --guard on                         # with guardrails
    python harness.py --seeds 0 1 2 --kick-after 1 --guard on          # banana knocked away + guardrails
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import json
import math
import os
import platform
import sys
import time
from datetime import datetime

import mujoco
import numpy as np
import onnxruntime

from sim.duck_sim import Duck, ViewerClosed, TABLE_H, TABLE_HALF
from agent_harness.franky_agent import OllamaBrain, RuleBrain, TOOLS, SYSTEM
from agent_harness.guardrails import CTX_WARN, GuardedTools, Guard, LLMError, chat_with_retry, look_exchange
from agent_harness.run_harness import RunLogger, truth, ollama_info, file_hash, tool_ok
from agent_harness.state_tracker import StateTracker
from project_paths import log_dir


# =============================================================================== the logger, with the new columns
def _insert_before(cols, name, new):
    i = cols.index(name)
    return cols[:i] + new + cols[i:]


class Logger(RunLogger):
    STEP_COLS = _insert_before(RunLogger.STEP_COLS, "result",
                               ["effect_ok", "expected", "actual", "loop", "unexpected", "external", "changes", "guard"]) + ["panel_text"]
    EP_COLS = _insert_before(RunLogger.EP_COLS, "llm_s",
                             ["panel", "guard", "failed_effects", "loop_flags", "unexpected_events", "external_changes", "done_rejected"])


# =============================================================================== fault injection
def kick_banana(duck, dist=0.20):
    """FAULT: knock the banana `dist` m away to another free spot on the table. Returns the new (x, y) or None."""
    b, g = duck.banana_pos(), duck.box_pos()
    best = None
    for k in range(8):
        ang = k * math.pi / 4
        x, y = b[0] + dist * math.cos(ang), b[1] + dist * math.sin(ang)
        if abs(x) > TABLE_HALF[0] - 0.08 or abs(y) > TABLE_HALF[1] - 0.08:
            continue                                       # too close to the table edge
        far = math.hypot(x - g[0], y - g[1])
        if far >= 0.12 and (best is None or far > best[0]):
            best = (far, x, y)                             # prefer the spot farthest from the green box
    if best is None:
        return None
    d = duck.data
    d.qpos[duck.ban_q:duck.ban_q + 3] = [best[1], best[2], TABLE_H + 0.011]
    d.qpos[duck.ban_q + 3:duck.ban_q + 7] = [1, 0, 0, 0]
    d.qvel[duck.ban_v:duck.ban_v + 6] = 0.0
    mujoco.mj_forward(duck.model, d)
    return best[1], best[2]

# =============================================================================== one episode, fully logged
def run_episode(duck, brain, seed, log, ep, max_steps=14, panel=False, kick_after=0, guard_on=False, verbose=True):
    duck.reset(seed=seed)
    tools = GuardedTools(duck, guard_on)
    tracker = StateTracker(duck, tools, truth)
    guard = Guard(guard_on)
    sim0, wall0 = duck.t, time.time()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Task: put the banana into the green box.\nYou look around:\n" + tools.observe()}]
    log.event("episode_start", ep, t_sim=0.0, seed=seed, panel=panel, guard=guard_on, kick_after=kick_after,
              truth=truth(duck), messages=msgs)
    sent, llm_s, ptok, gtok, bad, failed, nsteps, gtrig, reason = 0, 0.0, 0, 0, 0, 0, 0, 0, "max_steps"
    kicked = None
    keys = ("banana_in_box", "banana_x", "banana_y", "banana_z", "duck_x", "duck_y", "duck_yaw_deg",
            "tilt_deg", "fallen", "off_table", "box_x", "box_y")

    def note(i, name, text):
        """one guardrail action: an event in the log + a line on the screen"""
        nonlocal gtrig
        gtrig += 1
        log.event("guard", ep, i, duck.t - sim0, name=name, text=text)
        if verbose:
            print(f"           ## guard: {name} - {text}")

    for i in range(1, max_steps + 1):
        # --- did the world change by itself since the last action?
        ext = tracker.sense()
        if ext:
            log.event("scene_change", ep, i, duck.t - sim0, changes=ext, told_llm=("guard" if guard_on else "panel" if panel else "no"))
            if verbose:
                print(f"  ** scene change: {'; '.join(ext)}" + ("   (fresh look given)" if guard_on else "   (told to the LLM)" if panel else "   (LLM not told)"))
            if guard_on:
                msgs += look_exchange("SCENE CHANGE since your last action: " + "; ".join(ext) + ".", tools)
                note(i, "fresh_look", "scene change")
            elif panel:
                msgs.append({"role": "user", "content": tracker.change_message(ext)})
        log.event("llm_request", ep, i, duck.t - sim0, new_messages=msgs[sent:], n_messages=len(msgs))
        t0 = time.time()
        try:
            resp = chat_with_retry(brain, msgs, TOOLS)
        except LLMError as e:
            reason = "llm_error"
            log.event("llm_error", ep, i, duck.t - sim0, error=str(e))
            if verbose:
                print(f"  !! the LLM call failed twice: {e}")
            break
        dt = time.time() - t0
        sent = len(msgs)
        llm_s += dt
        m = resp.get("message", {})
        pt, gt = resp.get("prompt_eval_count"), resp.get("eval_count")
        ptok += pt or 0
        gtok += gt or 0
        pe_ms = round(resp.get("prompt_eval_duration", 0) / 1e6, 1)
        gen_ms = round(resp.get("eval_duration", 0) / 1e6, 1)
        log.event("llm_response", ep, i, duck.t - sim0, llm_s=round(dt, 3), message=m, prompt_tokens=pt, gen_tokens=gt,
                  prompt_eval_ms=pe_ms, gen_ms=gen_ms, done_reason=resp.get("done_reason"))
        ctx = getattr(brain, "num_ctx", None)
        if ctx and pt and pt >= CTX_WARN * ctx:               # Ollama would silently cut the start of the prompt
            log.event("context_warning", ep, i, duck.t - sim0, prompt_tokens=pt, num_ctx=ctx)
            if verbose:
                print(f"           !! the prompt ({pt} tokens) nearly fills the context window ({ctx})")
        calls = m.get("tool_calls") or []
        common = dict(episode=ep, seed=seed, step=i, llm_s=round(dt, 3), prompt_tokens=pt, gen_tokens=gt,
                      prompt_eval_ms=pe_ms, gen_ms=gen_ms, done_reason=resp.get("done_reason"))
        if not calls:                                     # the model talked instead of calling a tool
            bad += 1
            tr = tracker.now
            log.event("no_tool_call", ep, i, duck.t - sim0, text=m.get("content"))
            log.step_row(tool="(none)", args="", tool_ok=False, result=(m.get("content") or "")[:300].replace("\n", " | "),
                         sim_s=0, t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2),
                         holding_before=tr["holding"], holding_after=tr["holding"], external="; ".join(ext),
                         **{k: tr[k] for k in keys}, **common)
            if verbose:
                print(f"  step {i:2d} [{dt:4.1f}s]  (no tool call) {(m.get('content') or '')[:80]!r}")
            if bad >= 3:
                reason = "no_tool_calls"
                break
            msgs.append({"role": "assistant", "content": m.get("content") or ""})
            msgs.append({"role": "user", "content": "Reply by calling exactly one tool."})
            continue
        fn = calls[0].get("function", {})
        name, args = fn.get("name"), fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        s0 = duck.t
        log.event("tool_call", ep, i, duck.t - sim0, tool=name, args=args)
        result = tools.call(name, args)
        v = tracker.after_call(name, args)                     # <- expected vs actual, unexpected, loop
        before, after = v["before"], v["after"]
        ok = tool_ok(result)                                   # the old text-only check, kept for comparison
        failed += (not ok)
        nsteps += 1
        # --- guardrails: what the tools refused + what the guard decides now (applied after the step is logged)
        fired = [t["guard"] for t in tools.triggers]
        for t in tools.triggers:
            note(i, t["guard"], t["detail"])
        tools.triggers.clear()
        action, info = guard.after_call(v, tracker.counts["failed_effects"], duck.t - sim0)
        if action == "look":
            fired.append("loop_warning")
        elif action == "abort":
            fired.append(info)
        text = result
        if panel and name != "done":
            text = result + "\n" + tracker.panel(v)
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [calls[0]]})
        msgs.append({"role": "tool", "tool_name": name, "content": text})
        check = {k: v[k] for k in ("call", "expected", "actual", "effect_ok", "unexpected", "changes", "repeat", "loop")}
        log.event("tool_result", ep, i, duck.t - sim0, tool=name, ok=ok, result=result, check=check,
                  panel_text=text[len(result):].strip(), sim_s=round(duck.t - s0, 2), truth_before=before, truth_after=after)
        log.step_row(tool=name, args=json.dumps(args), tool_ok=ok, effect_ok=v["effect_ok"], expected=v["expected"],
                     actual=v["actual"], loop=v["loop"], unexpected="; ".join(v["unexpected"]), external="; ".join(ext),
                     changes="; ".join(v["changes"]), guard="; ".join(fired), result=result.replace("\n", " | "),
                     sim_s=round(duck.t - s0, 2), t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2),
                     holding_before=before["holding"], holding_after=after["holding"],
                     panel_text=text[len(result):].strip().replace("\n", " | "),
                     **{k: after[k] for k in keys}, **common)
        if verbose:
            shown = ", ".join(f"{k}={val}" for k, val in args.items())
            print(f"  step {i:2d} [{dt:4.1f}s]  {name}({shown})  ->  {result.splitlines()[0][:100]}")
            if not v["effect_ok"]:
                print(f"           !! goal missed: expected {v['expected']}; got {v['actual']}")
            if v["unexpected"]:
                print(f"           !! unexpected: {'; '.join(v['unexpected'])}")
            if v["loop"]:
                print(f"           !! loop: {v['loop']} (x{v['repeat']})")
        if tools.finished:
            reason = "done"
            break
        if duck.is_fallen() or duck.off_table():
            reason = "fell" if duck.is_fallen() else "off_table"
            break
        # --- fault test: knock the banana away (only if it lies on the table, not in the beak / box)
        if kick_after and kicked is None and nsteps >= kick_after and not duck.holding and not duck.banana_in_box():
            kicked = kick_banana(duck)
            if kicked:
                log.event("fault_injected", ep, i, duck.t - sim0, fault="banana_kick", new_xy=[round(c, 3) for c in kicked])
                if verbose:
                    print(f"  >> FAULT: the banana was knocked to ({kicked[0]:.2f}, {kicked[1]:.2f})")
        # --- now apply the guard's decision
        if action == "abort":
            reason = info
            note(i, info, "episode stopped")
            break
        if action == "look":
            msgs += look_exchange(info, tools)
            note(i, "loop_warning", info)
    final = truth(duck)
    success = bool(final["banana_in_box"])
    c = tracker.counts
    res = {"seed": seed, "success": success, "claimed_done": tools.finished, "false_claim": tools.finished and not success,
           "ended": reason, "steps": nsteps, "failed_calls": failed, "no_tool_calls": bad, "llm_s": round(llm_s, 1),
           "prompt_tokens": ptok, "gen_tokens": gtok, "sim_s": round(duck.t - sim0, 1), "wall_s": round(time.time() - wall0, 1),
           "final_banana_x": final["banana_x"], "final_banana_y": final["banana_y"],
           "final_dist_banana_to_box": round(math.hypot(final["banana_x"] - final["box_x"], final["banana_y"] - final["box_y"]), 3),
           "memory": "off", "faults": f"banana_kick@{kick_after}" if kicked else "none", "guardrail_triggers": gtrig,
           "panel": "on" if panel else "off", "guard": "on" if guard_on else "off", "done_rejected": tools.done_rejected, **c}
    log.event("episode_end", ep, t_sim=duck.t - sim0, result=res, truth=final)
    log.episode_row(episode=ep, model=brain.model, brain=type(brain).__name__, **res)
    return res

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--seeds", type=int, nargs="*", default=[0])
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--panel", choices=["on", "off"], default="off", help="tell the LLM the tracker's status text")
    ap.add_argument("--guard", choices=["on", "off"], default="off", help="switch the guardrails on")
    ap.add_argument("--kick-after", type=int, default=0, help="fault test: knock the banana away after this tool call")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true", help="also open the MuJoCo window (real time)")
    ap.add_argument("--logdir", default=log_dir("harness_v1"))
    args = ap.parse_args()
    panel, guard_on = args.panel == "on", args.guard == "on"

    brain = RuleBrain() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {brain.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")

    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    log = Logger(args.logdir, brain.model.replace(":", "-") + ("_panel" if panel else "") + ("_guard" if guard_on else ""))
    log.write_json("run_meta.json", {
        "run_id": log.run_id, "started": datetime.now().isoformat(timespec="seconds"), "command": " ".join(sys.argv),
        "brain": type(brain).__name__, "model": brain.model, "seeds": args.seeds, "max_steps": args.max_steps,
        "panel": args.panel, "guard": args.guard, "kick_after": args.kick_after,
        "llm_options": None if args.brain == "rules" else {"temperature": 0, "num_ctx": brain.num_ctx, "num_predict": 300, "think": False},
        "ollama": None if args.brain == "rules" else ollama_info(args.host, args.model),
        "software": {"python": sys.version.split()[0], "platform": platform.platform(), "mujoco": mujoco.__version__,
                     "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
        "code_hashes": {n: file_hash(n) for n in ("duck_sim.py", "franky_agent.py", "run_harness.py", "state_tracker.py",
                                                   "guardrails.py", "harness.py")},
        "system_prompt": SYSTEM, "tools": TOOLS,
    })
    results = []
    try:
        for ep, seed in enumerate(args.seeds, 1):
            print(f"\n=== episode {ep}  seed {seed}  (brain: {brain.model}, panel {args.panel}, guard {args.guard}) ===")
            r = run_episode(duck, brain, seed, log, ep, args.max_steps, panel, args.kick_after, guard_on)
            results.append(r)
            claim = "said done" if r["claimed_done"] else "did NOT say done"
            print(f"  RESULT: banana in box = {r['success']}   ({claim}; ended: {r['ended']}; {r['steps']} tool calls, "
                  f"{r['failed_effects']} missed goals, {r['loop_flags']} loop flags, {r['guardrail_triggers']} guard actions, "
                  f"LLM {r['llm_s']} s, sim {r['sim_s']} s, wall {r['wall_s']} s)")
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    finally:
        if results:
            n, w = len(results), sum(r["success"] for r in results)
            summ = {"run_id": log.run_id, "episodes": n, "successes": w, "success_rate": round(w / n, 3),
                    "false_claims": sum(r["false_claim"] for r in results),
                    "missed_goals": sum(r["failed_effects"] for r in results),
                    "loop_flags": sum(r["loop_flags"] for r in results),
                    "guard_actions": sum(r["guardrail_triggers"] for r in results),
                    "mean_steps": round(sum(r["steps"] for r in results) / n, 2),
                    "mean_llm_s": round(sum(r["llm_s"] for r in results) / n, 1),
                    "mean_sim_s": round(sum(r["sim_s"] for r in results) / n, 1)}
            log.write_json("run_summary.json", summ)
            print(f"\nSUCCESS {w}/{n}   mean tool calls {summ['mean_steps']}   mean LLM time {summ['mean_llm_s']} s   "
                  f"mean sim time {summ['mean_sim_s']} s   missed goals {summ['missed_goals']}   loop flags {summ['loop_flags']}"
                  f"   guard actions {summ['guard_actions']}")
        print(f"\nlog folder: {log.dir}")
        print(f"look at it:  Import-Csv {os.path.join(log.dir, 'episodes.csv')} | Format-Table")
        log.close()
        duck.close()


if __name__ == "__main__":
    main()
