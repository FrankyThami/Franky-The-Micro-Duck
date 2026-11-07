"""experiment.py - Harness v1, part A4: does Franky get better by writing lessons for himself?

  1. TRAIN  seeds : after an episode that went wrong or wasted steps, Franky is asked for ONE rule for his future self.
                    The rules go into a notebook (memory/lessons_<model>.json) - plain text you can read and edit.
  2. TEST   seeds : DIFFERENT seeds, run twice - memory OFF, then memory ON (the notebook is added to the system prompt).
  The weights never change. Train and test seeds are different on purpose, so we measure learning, not memorising.

    python agent_harness/experiment.py --train 0 1 2 3 4 --test 5 6 7 8 9 --kick-after 1
    python agent_harness/experiment.py --test 5 6 7 8 9 --kick-after 1            # reuse the notebook that already exists
    python agent_harness/experiment.py --brain rules --train 0 1 --test 2 3 --kick-after 1    # no LLM: checks the plumbing
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import os
import platform
import sys
import time
from datetime import datetime

import mujoco
import numpy as np
import onnxruntime

from agent_harness import harness
from sim.duck_sim import Duck, ViewerClosed
from agent_harness.franky_agent import OllamaBrain, RuleBrain, TOOLS, SYSTEM
from agent_harness.memory import LessonStore, MemoryBrain, reflect
from agent_harness.run_harness import ollama_info, file_hash
from project_paths import MEMORY_DIR, log_dir


class ExpLogger(harness.Logger):
    """The harness logger, but the 'memory' and 'brain' columns tell the truth for this phase."""

    def __init__(self, root, label, memory_label, brain_label):
        super().__init__(root, label)
        self.memory_label, self.brain_label = memory_label, brain_label

    def event(self, kind, episode=None, step=None, t_sim=None, **data):
        if kind == "episode_end":
            data["result"] = {**data["result"], "memory": self.memory_label}
        super().event(kind, episode, step, t_sim, **data)

    def episode_row(self, **row):
        row["memory"], row["brain"] = self.memory_label, self.brain_label
        super().episode_row(**row)


def write_meta(log, args, brain, phase, seeds, memory_label, lessons):
    log.write_json("run_meta.json", {
        "run_id": log.run_id, "started": datetime.now().isoformat(timespec="seconds"), "command": " ".join(sys.argv),
        "phase": phase, "brain": type(brain).__name__, "model": brain.model, "seeds": seeds, "max_steps": args.max_steps,
        "panel": args.panel, "guard": args.guard, "kick_after": args.kick_after, "memory": memory_label,
        "lessons_shown": lessons,
        "llm_options": None if args.brain == "rules" else {"temperature": 0, "num_ctx": brain.num_ctx, "num_predict": 300, "think": False},
        "ollama": None if args.brain == "rules" else ollama_info(args.host, args.model),
        "software": {"python": sys.version.split()[0], "platform": platform.platform(), "mujoco": mujoco.__version__,
                     "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
        "code_hashes": {n: file_hash(n) for n in ("duck_sim.py", "franky_agent.py", "run_harness.py", "state_tracker.py",
                                                   "guardrails.py", "harness.py", "memory.py", "experiment.py")},
        "system_prompt": SYSTEM, "tools": TOOLS,
    })


def summarize(log, results):
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
    return summ


def run_phase(name, args, duck, base_brain, seeds, store, learn):
    """One folder of logs. store=None -> memory OFF. learn=True -> reflect after each bad episode (train phase)."""
    memory_label = "off" if store is None else ("on" if not learn else "learning")
    mbrain = MemoryBrain(base_brain, store, args.k)
    log = ExpLogger(args.logdir, f"{base_brain.model.replace(':', '-')}_{name}", memory_label, type(base_brain).__name__)
    write_meta(log, args, base_brain, name, seeds, memory_label, mbrain.lessons_shown())
    results = []
    print(f"\n########  phase: {name}   (memory {memory_label}, seeds {seeds})  ########")
    try:
        for ep, seed in enumerate(seeds, 1):
            shown = mbrain.lessons_shown()
            print(f"\n=== episode {ep}  seed {seed}  ({name}) ===")
            for t in shown:
                print(f"  [notebook] {t}")
            log.event("memory_used", ep, memory=memory_label, lessons=shown)
            r = harness.run_episode(duck, mbrain, seed, log, ep, args.max_steps, args.panel == "on", args.kick_after,
                                    args.guard == "on")
            results.append(r)
            print(f"  RESULT: banana in box = {r['success']}   ({r['steps']} tool calls, {r['failed_effects']} missed goals, "
                  f"ended: {r['ended']}, LLM {r['llm_s']} s, sim {r['sim_s']} s)")
            if learn:
                out = reflect(base_brain, store, mbrain.last_msgs, r, log.run_id)
                log.event("reflection", ep, **out)
                print(f"  [reflection] {out['status']}" + (f": {out['lesson']}  (seen {out['count']}x)" if "lesson" in out else ""))
    finally:
        summ = summarize(log, results) if results else None
        log.close()
    return summ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--train", type=int, nargs="*", default=[], help="seeds Franky learns from")
    ap.add_argument("--test", type=int, nargs="*", default=[], help="different seeds, run with memory OFF and ON")
    ap.add_argument("--keep-notebook", action="store_true", help="do not start the notebook empty before training")
    ap.add_argument("--no-baseline", action="store_true", help="skip the memory-OFF test run")
    ap.add_argument("--k", type=int, default=3, help="how many lessons go into the prompt")
    ap.add_argument("--memory-file", default=None)
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--panel", choices=["on", "off"], default="off")
    ap.add_argument("--guard", choices=["on", "off"], default="off")
    ap.add_argument("--kick-after", type=int, default=0)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true")
    ap.add_argument("--logdir", default=log_dir("harness_v1"))
    args = ap.parse_args()
    if not args.train and not args.test:
        raise SystemExit("Give --train SEEDS and/or --test SEEDS.")
    if set(args.train) & set(args.test):
        raise SystemExit("Train and test seeds must be different (otherwise we only measure memorising).")

    base = RuleBrain() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {base.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")
    path = args.memory_file or os.path.join(MEMORY_DIR, f"lessons_{base.model.replace(':', '-')}.json")
    store = LessonStore(path, fresh=bool(args.train) and not args.keep_notebook)
    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    summaries = {}
    try:
        if args.train:
            summaries["train"] = run_phase("train", args, duck, base, args.train, store, learn=True)
        if args.test:
            if not store.lessons:
                print("\n(the notebook is empty - the memory-ON run will be the same as memory OFF)")
            if not args.no_baseline:
                summaries["test, memory OFF"] = run_phase("test_mem-off", args, duck, base, args.test, None, learn=False)
            summaries["test, memory ON"] = run_phase("test_mem-on", args, duck, base, args.test, store, learn=False)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    finally:
        duck.close()

    print("\n" + "=" * 100)
    print(f"{'phase':<20}{'success':>9}{'steps':>8}{'missed':>8}{'llm_s':>8}{'sim_s':>8}")
    for k, s in summaries.items():
        if s:
            print(f"{k:<20}{str(s['successes']) + '/' + str(s['episodes']):>9}{s['mean_steps']:>8}{s['missed_goals']:>8}"
                  f"{s['mean_llm_s']:>8}{s['mean_sim_s']:>8}")
    print("=" * 100)
    print(f"notebook: {path}")
    for l in store.top(10):
        print(f"  ({l['count']}x) {l['text']}")


if __name__ == "__main__":
    main()