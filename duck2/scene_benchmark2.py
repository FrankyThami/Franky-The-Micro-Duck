"""scene_benchmark2.py - run one duck on the NEW scenes N0 - N9 (scenes2.py): a blue box it must not use, two bananas,
a banana that is already done, no banana at all...

    python duck2/scene_benchmark2.py --brain rules                       # NO LLM: what does a recipe-follower score on these scenes?
    python duck2/scene_benchmark2.py                                     # Franky (gemma4:e2b) on all 10 scenes, seeing ONLY through his camera
    python duck2/scene_benchmark2.py --view                              # all scenes, WITH the MuJoCo window (real time)
    python duck2/scene_benchmark2.py --scenes 4 5 --view                 # only scenes N4 and N5, with the MuJoCo window
    python duck2/scene_benchmark2.py --model qwen3-vl:2b-instruct        # the second duck (Thami)
    python duck2/scene_benchmark2.py --perception oracle                 # "perfect eyes" (for comparison)
    python duck2/scene_benchmark2.py --record                            # all scenes, and SAVE the MuJoCo footage as an mp4 (no window)
    python duck2/scene_benchmark2.py --view --record                     # the same, while you also watch the MuJoCo window

Nothing of the old test set is touched: this script has its own camera (eye2.py), body (duck2.py), tools + referee (tools2.py),
scenes (scenes2.py) and episode loop. It writes its logs to outputs\\logs\\duck2\\run_<date>_<model>_scenes2_...\\ (events.jsonl, steps.csv,
episodes.csv, run_meta.json, run_summary.json) in the same layout as the old runs. The videos use recorder.py as it is.
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

from duck2 import eye2
from duck2 import scenes2
from duck2 import tools2
from duck2.duck2 import Duck2
from sim.duck_sim import ViewerClosed
from agent_harness.franky_agent import OllamaBrain
from agent_harness.guardrails import LLMError, chat_with_retry
from sim.recorder import Recorder
from agent_harness.run_harness import BAD_START, RunLogger, file_hash, ollama_info
from project_paths import log_dir, video_dir


# =============================================================================== the log
class Logger2(RunLogger):
    STEP_COLS = ["run_id", "episode", "scene", "step", "tool", "args", "tool_ok", "effect_ok", "expected", "actual",
                 "unexpected", "loop", "result", "llm_s", "prompt_tokens", "gen_tokens", "prompt_eval_ms", "gen_ms",
                 "done_reason", "sim_s", "t_sim", "t_run", "holding_after", "n_green", "n_blue", "n_table", "n_lost",
                 "banana1", "banana2", "duck_x", "duck_y", "duck_yaw_deg", "tilt_deg", "fallen", "off_table"]
    EP_COLS = ["run_id", "episode", "scene", "name", "model", "brain", "success", "ended", "claimed", "false_claim", "steps",
               "failed_effects", "moves", "n_bananas", "n_green", "n_blue", "placed_blue", "first_pick", "picked_order", "placed", "note",
               "failed_calls", "no_tool_calls", "llm_s", "prompt_tokens", "gen_tokens", "sim_s", "wall_s"]


# =============================================================================== the caption (window + video footer)
class CaptionBrain:
    """Wraps a brain. With a window open it writes the scene, the last tool result and the tool Franky just chose at the
    bottom of the MuJoCo window; --record gives the video footer the same lines. The answers of the brain are not changed."""

    def __init__(self, brain, duck):
        self.brain, self.duck = brain, duck
        self.model = brain.model
        self.num_ctx = getattr(brain, "num_ctx", None)
        self.title, self.n = "", 0
        self.first = None                                       # the first tool Franky chose in the current scene
        self.rec = None

    def warmup(self):
        return self.brain.warmup()

    def show(self, *lines):
        if getattr(self.duck, "_fast", False):                  # only the fast window has a caption
            self.duck.viewer.caption = "\n".join(l for l in lines if l)
        if self.rec is not None:
            self.rec.set_caption([l for l in lines if l], self.n)

    def chat(self, msgs, tools=None, **kw):
        self.n += 1
        last = msgs[-1]["content"].splitlines()[0][:110] if msgs and msgs[-1]["role"] == "tool" else ""
        self.show(self.title, f"step {self.n}: Franky is thinking...", "last result: " + last if last else "")
        resp = self.brain.chat(msgs, tools, **kw)
        calls = resp.get("message", {}).get("tool_calls") or []
        if calls:
            fn = calls[0]["function"]
            if self.n == 1:
                self.first = fn.get("name")
            vals = ", ".join(str(v) for v in (fn.get("arguments") or {}).values())
            act = f"{fn.get('name')}({vals})" if vals else str(fn.get("name"))
            self.show(self.title, f"step {self.n}: {act}", "last result: " + last if last else "")
        return resp


# =============================================================================== one scene
def reset_scene(duck, sid, sc):
    return duck.reset(seed=sid, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
                      bananas=[dict(xy=b["xy"], yaw=math.radians(b.get("yaw", 0.0))) for b in sc["bananas"]],
                      green_xy=sc["green"], blue_xy=sc.get("blue"))


def _short(b):
    return f"{b['loc']}({b['x']:+.2f},{b['y']:+.2f})"


def run_episode2(duck, brain, sid, sc, log, ep, max_steps=24, verbose=True):
    reset_scene(duck, sid, sc)
    tools = tools2.EyeTools2(duck)
    ref = tools2.Referee2(duck, tools, sc)
    sim0, wall0 = duck.t, time.time()
    msgs = [{"role": "system", "content": tools2.EYE2_SYSTEM},
            {"role": "user", "content": f"Task: {sc['task']}\nYou look around:\n" + tools.observe()}]
    log.event("episode_start", ep, t_sim=0.0, scene=sid, name=sc["name"], task=sc["task"], goal=sc["goal"],
              truth=tools2.truth2(duck), messages=msgs)
    sent, llm_s, ptok, gtok, bad, failed, nsteps, reason = 0, 0.0, 0, 0, 0, 0, 0, "max_steps"
    seq = []

    for i in range(1, max_steps + 1):
        log.event("llm_request", ep, i, duck.t - sim0, new_messages=msgs[sent:], n_messages=len(msgs))
        t0 = time.time()
        try:
            resp = chat_with_retry(brain, msgs, tools2.EYE2_TOOLS)
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
        calls = m.get("tool_calls") or []
        common = dict(episode=ep, scene=sid, step=i, llm_s=round(dt, 3), prompt_tokens=pt, gen_tokens=gt,
                      prompt_eval_ms=pe_ms, gen_ms=gen_ms, done_reason=resp.get("done_reason"))
        tr = ref.now
        state = dict(n_green=tr["n_green"], n_blue=tr["n_blue"], n_table=tr["n_table"], n_lost=tr["n_lost"],
                     banana1=_short(tr["bananas"][0]) if len(tr["bananas"]) > 0 else "",
                     banana2=_short(tr["bananas"][1]) if len(tr["bananas"]) > 1 else "",
                     duck_x=tr["duck_x"], duck_y=tr["duck_y"], duck_yaw_deg=tr["duck_yaw_deg"], tilt_deg=tr["tilt_deg"],
                     fallen=tr["fallen"], off_table=tr["off_table"], holding_after=tr["holding"])
        if not calls:                                     # the model talked instead of calling a tool
            bad += 1
            log.event("no_tool_call", ep, i, duck.t - sim0, text=m.get("content"))
            log.step_row(tool="(none)", args="", tool_ok=False, result=(m.get("content") or "")[:300].replace("\n", " | "),
                         sim_s=0, t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2), **state, **common)
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
        v = ref.after_call(name, args)
        ok = not result.startswith(BAD_START)
        failed += (not ok)
        nsteps += 1
        seq.append(f"{name}({', '.join(str(x) for x in args.values())})" if args else name)
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [calls[0]]})
        msgs.append({"role": "tool", "tool_name": name, "content": result})
        after = v["after"]
        log.event("tool_result", ep, i, duck.t - sim0, tool=name, ok=ok, result=result,
                  check={k: v[k] for k in ("call", "expected", "actual", "effect_ok", "unexpected", "loop")},
                  sim_s=round(duck.t - s0, 2), truth_before=v["before"], truth_after=after)
        log.step_row(tool=name, args=json.dumps(args), tool_ok=ok, effect_ok=v["effect_ok"], expected=v["expected"],
                     actual=v["actual"], unexpected="; ".join(v["unexpected"]), loop=v["loop"],
                     result=result.replace("\n", " | "), sim_s=round(duck.t - s0, 2), t_sim=round(duck.t - sim0, 2),
                     t_run=round(time.time() - log.t0, 2),
                     **dict(state, n_green=after["n_green"], n_blue=after["n_blue"], n_table=after["n_table"], n_lost=after["n_lost"],
                            banana1=_short(after["bananas"][0]) if len(after["bananas"]) > 0 else "",
                            banana2=_short(after["bananas"][1]) if len(after["bananas"]) > 1 else "",
                            duck_x=after["duck_x"], duck_y=after["duck_y"], duck_yaw_deg=after["duck_yaw_deg"],
                            tilt_deg=after["tilt_deg"], fallen=after["fallen"], off_table=after["off_table"],
                            holding_after=after["holding"]), **common)
        if verbose:
            shown = ", ".join(f"{k}={val}" for k, val in args.items())
            print(f"  step {i:2d} [{dt:4.1f}s]  {name}({shown})  ->  {result.splitlines()[0][:100]}")
            if not v["effect_ok"]:
                print(f"           !! goal missed: expected {v['expected']}; got {v['actual']}")
            if v["unexpected"]:
                print(f"           !! unexpected: {'; '.join(v['unexpected'])}")
        if tools.finished:
            reason = tools.finished
            break
        if duck.is_fallen() or duck.off_table():
            reason = "fell" if duck.is_fallen() else "off_table"
            break
    success, note, extras, final = ref.final()
    res = {"scene": sid, "name": sc["name"], "success": bool(success), "ended": reason, "steps": nsteps,
           "failed_effects": ref.counts["failed_effects"], "failed_calls": failed, "no_tool_calls": bad, "llm_s": round(llm_s, 1),
           "prompt_tokens": ptok, "gen_tokens": gtok, "sim_s": round(duck.t - sim0, 1), "wall_s": round(time.time() - wall0, 1),
           "note": note, "sequence": seq, **extras}
    log.event("episode_end", ep, t_sim=duck.t - sim0, result=res, truth=final)
    log.episode_row(episode=ep, model=brain.model, brain=type(brain).__name__,
                    **{**res, "first_pick": json.dumps(res["first_pick"]), "picked_order": json.dumps(res["picked_order"]),
                       "placed": json.dumps(res["placed"])})
    return res


# =============================================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--perception", choices=["camera", "oracle"], default="camera",
                    help="camera = the duck sees only through its head camera; oracle = perfect eyes (for comparison)")
    ap.add_argument("--scenes", type=int, nargs="*", default=None, help="scene numbers 0-9 (default: all)")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true")
    ap.add_argument("--record", action="store_true", help="save the MuJoCo footage of the run as an mp4 (see --video-dir)")
    ap.add_argument("--rec-fps", type=int, default=30, help="frames per second of the saved video (default 30)")
    ap.add_argument("--video-dir", default=video_dir("duck2"))
    ap.add_argument("--logdir", default=log_dir("duck2"))
    args = ap.parse_args()
    S = scenes2.SCENES2
    ids = list(range(len(S))) if args.scenes is None else args.scenes
    bad = [i for i in ids if not 0 <= i < len(S)]
    if bad:
        raise SystemExit(f"No such scene: {bad}. Scenes are 0 to {len(S) - 1}.")

    brain = tools2.EyeRuleBrain2() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {brain.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")

    duck = Duck2("Franky", viewer=args.view, realtime=args.view)
    duck.eye = eye2.Eye2(duck) if args.perception == "camera" else eye2.OracleEye2(duck)
    cap = CaptionBrain(brain, duck)
    label = f"{brain.model.replace(':', '-')}_scenes2_{'eye' if args.perception == 'camera' else 'oracle'}"
    rec = Recorder(duck, args.video_dir, f"{label}_{time.strftime('%Y%m%d_%H%M%S')}", args.rec_fps) if args.record else None
    cap.rec = rec
    log = Logger2(args.logdir, label)
    log.write_json("run_meta.json", {
        "run_id": log.run_id, "started": datetime.now().isoformat(timespec="seconds"), "command": " ".join(sys.argv),
        "brain": type(brain).__name__, "model": brain.model, "perception": args.perception, "scenes": ids,
        "max_steps": args.max_steps,
        "llm_options": None if args.brain == "rules" else {"temperature": 0, "num_ctx": brain.num_ctx, "num_predict": 300, "think": False},
        "ollama": None if args.brain == "rules" else ollama_info(args.host, args.model),
        "software": {"python": sys.version.split()[0], "platform": platform.platform(), "mujoco": mujoco.__version__,
                     "numpy": np.__version__},
        "code_hashes": {n: file_hash(n) for n in ("duck_sim.py", "duck2.py", "eye.py", "eye2.py", "tools2.py", "scenes2.py",
                                                   "scene_benchmark2.py")},
        "system_prompt": tools2.EYE2_SYSTEM, "tools": tools2.EYE2_TOOLS})
    log.write_json("scenes2.json", {f"N{i}": S[i] for i in ids})
    results, firsts = [], {}
    try:
        for ep, sid in enumerate(ids, 1):
            sc = S[sid]
            print(f"\n=== scene N{sid}: {sc['name']}   ({sc['note']}) ===")
            print(f"    task: {sc['task']}")
            cap.title, cap.n = f"SCENE N{sid}: {sc['name']}", 0
            cap.first = None
            if rec:
                rec.mark(f"scene N{sid}: {sc['name']}")
            cap.show(cap.title)
            r = run_episode2(duck, cap, sid, sc, log, ep, args.max_steps)
            results.append(r)
            firsts[sid] = cap.first
            print(f"  RESULT: solved = {r['success']}   ({r['steps']} tool calls, {r['failed_effects']} missed goals, "
                  f"ended: {r['ended']}, LLM {r['llm_s']} s, sim {r['sim_s']} s)")
            print(f"  what happened: {r['note']}")
            if r["first_pick"] and r["n_bananas"] > 1:
                fp = r["first_pick"]
                print(f"  it picked banana {fp['banana']} first ({'the nearer one' if fp['nearer_of_the_two'] else 'the farther one'}); "
                      f"placed {r['placed']}; ended with: {r['claimed'] or r['ended']}")
            if rec:
                rec.set_verdict(r["success"], r["sim_s"], r["steps"], r["llm_s"], r["ended"])
            if args.view:
                cap.show(cap.title, ("SUCCESSFUL" if r["success"] else "NOT SUCCESSFUL")
                         + f"   {r['sim_s']} s, {r['steps']} tool calls, ended: {r['ended']}")
            if args.view or rec:
                duck.hold(3.0)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    finally:
        summ = None
        if results:
            n = len(results)
            summ = {"run_id": log.run_id, "episodes": n, "successes": sum(r["success"] for r in results),
                    "false_claims": sum(r["false_claim"] for r in results),
                    "blue_box_mistakes": sum(r["placed_blue"] for r in results),
                    "missed_goals": sum(r["failed_effects"] for r in results),
                    "mean_steps": round(sum(r["steps"] for r in results) / n, 2),
                    "mean_llm_s": round(sum(r["llm_s"] for r in results) / n, 1),
                    "mean_sim_s": round(sum(r["sim_s"] for r in results) / n, 1)}
            log.write_json("run_summary.json", summ)
        log.close()
        if rec:
            rec.close()
        duck.eye.close()
        duck.close()

    print("\n" + "=" * 100)
    print(f"perception: {args.perception}     model: {brain.model}")
    print(f"{'scene':<7}{'name':<20}{'solved':<8}{'steps':>6}{'missed':>8}  {'first move':<12}{'ended':<10}")
    for r in results:
        sid = r["scene"]
        print(f"N{sid:<6}{r['name']:<20}{'YES' if r['success'] else 'NO':<8}{r['steps']:>6}{r['failed_effects']:>8}  "
              f"{str(firsts.get(sid)):<12}{r['ended']:<10}")
    print("-" * 100)
    print("what happened in each scene:")
    for r in results:
        print(f"  N{r['scene']} {r['name']}: {r['note']}")
        print(f"      tools: {' > '.join(r['sequence'])}")
    if summ:
        print("-" * 100)
        print(f"solved {summ['successes']}/{summ['episodes']}   blue-box mistakes {summ['blue_box_mistakes']}   "
              f"false 'done' claims {summ['false_claims']}   mean steps {summ['mean_steps']}   missed goals {summ['missed_goals']}   "
              f"LLM {summ['mean_llm_s']} s   sim {summ['mean_sim_s']} s per scene   "
              f"first move was observe in {sum(v == 'observe' for v in firsts.values())}/{len(firsts)}")
    print("=" * 100)
    print(f"log folder: {log.dir}")


if __name__ == "__main__":
    main()