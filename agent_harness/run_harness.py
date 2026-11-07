"""run_harness.py - Harness v1, part A1: the same agent loop as franky_agent.py, but with a COMPLETE LOG.

Every run gets its own folder:   outputs/logs/harness_v1/run_<date>_<time>_<model>/
    run_meta.json    what was run: model + Ollama version + model digest, options, seeds, prompt, tools, software versions
    events.jsonl     EVERY event in order (what the LLM was shown, what it answered, each tool call and result,
                     the true state of the world after each step) - one JSON object per line
    steps.csv        one row per tool call  (open it in Excel)
    episodes.csv     one row per episode    (open it in Excel)
    run_summary.json totals for the whole run

    python run_harness.py --brain rules --seeds 0 1       # no LLM: checks the harness itself
    python run_harness.py --seeds 0 1 2                   # Franky with gemma4:e2b
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import sys
import time
import urllib.request
from datetime import datetime

import mujoco
import numpy as np
import onnxruntime

from sim.duck_sim import Duck, ViewerClosed
from agent_harness.franky_agent import FrankyTools, OllamaBrain, RuleBrain, TOOLS, SYSTEM
from project_paths import find_code, log_dir

BAD_START = ("Cannot", "Failed", "Missed", "Unknown", "Bad arguments", "You have fallen", "You walked off")


def tool_ok(result):
    """A tool call 'worked' unless its answer starts with a failure word (simple check; part A2 improves it)."""
    return not result.startswith(BAD_START)


def truth(duck):
    """The TRUE state of the world, read from the simulator. Logged only - the LLM never sees this."""
    x, y, yaw, z = duck.pose()
    b, g = duck.banana_pos(), duck.box_pos()
    r = lambda v, n=4: round(float(v), n)
    return {"duck_x": r(x), "duck_y": r(y), "duck_yaw_deg": r(math.degrees(yaw), 1), "duck_z": r(z),
            "tilt_deg": r(duck.tilt_deg(), 1), "fallen": bool(duck.is_fallen()), "off_table": bool(duck.off_table()),
            "holding": bool(duck.holding),
            "banana_x": r(b[0]), "banana_y": r(b[1]), "banana_z": r(b[2]), "banana_in_box": bool(duck.banana_in_box()),
            "box_x": r(g[0]), "box_y": r(g[1])}


def file_hash(name):
    """Short fingerprint of one of the project's .py files (found by its name), so a log shows which code made it."""
    try:
        with open(find_code(name), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except (OSError, TypeError):
        return None


def ollama_info(host, model):
    """Ollama version and the exact model build (digest), so a result can always be traced to a model."""
    info = {}
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/version", timeout=5) as r:
            info["ollama_version"] = json.loads(r.read().decode()).get("version")
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=5) as r:
            for m in json.loads(r.read().decode()).get("models", []):
                if model in (m.get("name"), m.get("model")):
                    info.update(model_digest=m.get("digest"), model_size_bytes=m.get("size"), model_details=m.get("details"))
    except Exception as e:
        info["error"] = str(e)
    return info


# =============================================================================== the logger
class RunLogger:
    STEP_COLS = ["run_id", "episode", "seed", "step", "tool", "args", "tool_ok", "result",
                 "llm_s", "prompt_tokens", "gen_tokens", "prompt_eval_ms", "gen_ms", "done_reason",
                 "sim_s", "t_sim", "t_run", "holding_before", "holding_after", "banana_in_box",
                 "banana_x", "banana_y", "banana_z", "duck_x", "duck_y", "duck_yaw_deg", "tilt_deg",
                 "fallen", "off_table", "box_x", "box_y"]
    EP_COLS = ["run_id", "episode", "seed", "model", "brain", "success", "claimed_done", "false_claim", "ended",
               "steps", "failed_calls", "no_tool_calls", "llm_s", "prompt_tokens", "gen_tokens", "sim_s", "wall_s",
               "final_banana_x", "final_banana_y", "final_dist_banana_to_box", "memory", "faults", "guardrail_triggers"]

    def __init__(self, root, label):
        self.run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{label}"
        self.dir = os.path.join(root, self.run_id)
        os.makedirs(self.dir)
        self.t0, self.n = time.time(), 0
        self._ev = open(os.path.join(self.dir, "events.jsonl"), "w", encoding="utf-8")
        self._sf = open(os.path.join(self.dir, "steps.csv"), "w", newline="", encoding="utf-8-sig")
        self._steps = csv.DictWriter(self._sf, fieldnames=self.STEP_COLS, extrasaction="ignore", restval="")
        self._steps.writeheader()
        self._ef = open(os.path.join(self.dir, "episodes.csv"), "w", newline="", encoding="utf-8-sig")
        self._eps = csv.DictWriter(self._ef, fieldnames=self.EP_COLS, extrasaction="ignore", restval="")
        self._eps.writeheader()

    def event(self, kind, episode=None, step=None, t_sim=None, **data):
        rec = {"n": self.n, "ts": datetime.now().isoformat(timespec="milliseconds"),
               "t_run": round(time.time() - self.t0, 3), "kind": kind, "episode": episode, "step": step,
               "t_sim": None if t_sim is None else round(t_sim, 3)}
        rec.update(data)
        self.n += 1
        self._ev.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._ev.flush()                      # a crash or Ctrl+C never loses what already happened

    def step_row(self, **row):
        self._steps.writerow({"run_id": self.run_id, **row})
        self._sf.flush()

    def episode_row(self, **row):
        self._eps.writerow({"run_id": self.run_id, **row})
        self._ef.flush()

    def write_json(self, name, obj):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, default=str)

    def close(self):
        for f in (self._ev, self._sf, self._ef):
            f.close()


# =============================================================================== one episode, fully logged
def run_episode(duck, brain, seed, log, ep, max_steps=14, verbose=True):
    duck.reset(seed=seed)
    tools = FrankyTools(duck)
    sim0, wall0 = duck.t, time.time()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Task: put the banana into the green box.\nYou look around:\n" + tools.observe()}]
    log.event("episode_start", ep, t_sim=0.0, seed=seed, truth=truth(duck), messages=msgs)
    sent, llm_s, ptok, gtok, bad, failed, nsteps, reason = 0, 0.0, 0, 0, 0, 0, 0, "max_steps"
    for i in range(1, max_steps + 1):
        log.event("llm_request", ep, i, duck.t - sim0, new_messages=msgs[sent:], n_messages=len(msgs))
        t0 = time.time()
        resp = brain.chat(msgs, TOOLS)
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
        common = dict(episode=ep, seed=seed, step=i, llm_s=round(dt, 3), prompt_tokens=pt, gen_tokens=gt,
                      prompt_eval_ms=pe_ms, gen_ms=gen_ms, done_reason=resp.get("done_reason"))
        if not calls:                                     # the model talked instead of calling a tool
            bad += 1
            tr = truth(duck)
            log.event("no_tool_call", ep, i, duck.t - sim0, text=m.get("content"))
            log.step_row(tool="(none)", args="", tool_ok=False, result=(m.get("content") or "")[:300].replace("\n", " | "),
                         sim_s=0, t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2),
                         holding_before=tr["holding"], holding_after=tr["holding"], **{k: tr[k] for k in (
                             "banana_in_box", "banana_x", "banana_y", "banana_z", "duck_x", "duck_y", "duck_yaw_deg",
                             "tilt_deg", "fallen", "off_table", "box_x", "box_y")}, **common)
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
        before = truth(duck)
        s0 = duck.t
        log.event("tool_call", ep, i, duck.t - sim0, tool=name, args=args)
        result = tools.call(name, args)
        after = truth(duck)
        ok = tool_ok(result)
        failed += (not ok)
        nsteps += 1
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [calls[0]]})
        msgs.append({"role": "tool", "tool_name": name, "content": result})
        log.event("tool_result", ep, i, duck.t - sim0, tool=name, ok=ok, result=result,
                  sim_s=round(duck.t - s0, 2), truth_before=before, truth_after=after)
        log.step_row(tool=name, args=json.dumps(args), tool_ok=ok, result=result.replace("\n", " | "),
                     sim_s=round(duck.t - s0, 2), t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2),
                     holding_before=before["holding"], holding_after=after["holding"],
                     **{k: after[k] for k in ("banana_in_box", "banana_x", "banana_y", "banana_z", "duck_x", "duck_y",
                                              "duck_yaw_deg", "tilt_deg", "fallen", "off_table", "box_x", "box_y")}, **common)
        if verbose:
            shown = ", ".join(f"{k}={v}" for k, v in args.items())
            print(f"  step {i:2d} [{dt:4.1f}s]  {name}({shown})  ->  {result.splitlines()[0][:100]}")
        if tools.finished:
            reason = "done"
            break
        if duck.is_fallen() or duck.off_table():
            reason = "fell" if duck.is_fallen() else "off_table"
            break
    final = truth(duck)
    success = bool(final["banana_in_box"])
    res = {"seed": seed, "success": success, "claimed_done": tools.finished, "false_claim": tools.finished and not success,
           "ended": reason, "steps": nsteps, "failed_calls": failed, "no_tool_calls": bad, "llm_s": round(llm_s, 1),
           "prompt_tokens": ptok, "gen_tokens": gtok, "sim_s": round(duck.t - sim0, 1), "wall_s": round(time.time() - wall0, 1),
           "final_banana_x": final["banana_x"], "final_banana_y": final["banana_y"],
           "final_dist_banana_to_box": round(math.hypot(final["banana_x"] - final["box_x"], final["banana_y"] - final["box_y"]), 3),
           "memory": "off", "faults": "none", "guardrail_triggers": 0}
    log.event("episode_end", ep, t_sim=duck.t - sim0, result=res, truth=final)
    log.episode_row(episode=ep, model=brain.model, brain=type(brain).__name__, **res)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--seeds", type=int, nargs="*", default=[0])
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true", help="also open the MuJoCo window (real time)")
    ap.add_argument("--logdir", default=log_dir("harness_v1"))
    args = ap.parse_args()

    brain = RuleBrain() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {brain.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")

    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    log = RunLogger(args.logdir, brain.model.replace(":", "-"))
    log.write_json("run_meta.json", {
        "run_id": log.run_id, "started": datetime.now().isoformat(timespec="seconds"), "command": " ".join(sys.argv),
        "brain": type(brain).__name__, "model": brain.model, "seeds": args.seeds, "max_steps": args.max_steps,
        "llm_options": None if args.brain == "rules" else {"temperature": 0, "num_ctx": brain.num_ctx, "num_predict": 300, "think": False},
        "ollama": None if args.brain == "rules" else ollama_info(args.host, args.model),
        "software": {"python": sys.version.split()[0], "platform": platform.platform(), "mujoco": mujoco.__version__,
                     "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
        "code_hashes": {n: file_hash(n) for n in ("duck_sim.py", "franky_agent.py", "run_harness.py")},
        "system_prompt": SYSTEM, "tools": TOOLS,
    })
    results = []
    try:
        for ep, seed in enumerate(args.seeds, 1):
            print(f"\n=== episode {ep}  seed {seed}  (brain: {brain.model}) ===")
            r = run_episode(duck, brain, seed, log, ep, args.max_steps)
            results.append(r)
            claim = "said done" if r["claimed_done"] else "did NOT say done"
            print(f"  RESULT: banana in box = {r['success']}   ({claim}; ended: {r['ended']}; {r['steps']} tool calls, "
                  f"{r['failed_calls']} failed, LLM {r['llm_s']} s, sim {r['sim_s']} s, wall {r['wall_s']} s)")
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    finally:
        if results:
            n, w = len(results), sum(r["success"] for r in results)
            summ = {"run_id": log.run_id, "episodes": n, "successes": w, "success_rate": round(w / n, 3),
                    "false_claims": sum(r["false_claim"] for r in results),
                    "mean_steps": round(sum(r["steps"] for r in results) / n, 2),
                    "mean_llm_s": round(sum(r["llm_s"] for r in results) / n, 1),
                    "mean_sim_s": round(sum(r["sim_s"] for r in results) / n, 1)}
            log.write_json("run_summary.json", summ)
            print(f"\nSUCCESS {w}/{n}   mean tool calls {summ['mean_steps']}   mean LLM time {summ['mean_llm_s']} s   "
                  f"mean sim time {summ['mean_sim_s']} s")
        print(f"\nlog folder: {log.dir}")
        print(f"look at it:  Import-Csv {os.path.join(log.dir, 'episodes.csv')} | Format-Table")
        log.close()
        duck.close()


if __name__ == "__main__":
    main()


