"""scene_benchmark.py (v3) - run one duck on the fixed scenes in scenes.py and print a one-line verdict per scene.

    python duck1/scene_benchmark.py --brain rules                        # NO LLM: can a rule-follower solve the scenes with the camera?
    python duck1/scene_benchmark.py                                      # Franky (gemma4:e2b) on all 10 scenes, seeing ONLY through his camera
    python duck1/scene_benchmark.py --view                               # all scenes, WITH the MuJoCo window (real time)
    python duck1/scene_benchmark.py --scenes 5 6 --view                  # only scenes 5 and 6, with the MuJoCo window
    python duck1/scene_benchmark.py --perception oracle                  # "perfect eyes": the duck sees everything, everywhere (for comparison)
    python duck1/scene_benchmark.py --kick-after 1 --guard on            # same scenes, banana knocked away after step 1
    python duck1/scene_benchmark.py --model qwen3-vl:2b-instruct         # the second duck (Thami)
    python duck1/scene_benchmark.py --record                             # all scenes, and SAVE the MuJoCo footage as an mp4 (no window)
    python duck1/scene_benchmark.py --view --record                      # the same, while you also watch the MuJoCo window

harness.py is used as it is. Two things are swapped in before it runs (see eye_tools.py): the duck's tools, prompt and
referee (so the duck sees only what its camera shows, and its first move is its own `observe`), and the starting
layout (the scene number takes the place of the seed, so every log shows the scene number in the `seed` column).
A scene may also give the banana's direction: banana_yaw=<degrees> (0 = along the table's x axis).

--record saves the MuJoCo footage (recorder.py: one big view of the whole table, nothing drawn on top of it; under it a footer
with the scene / step and tool / last result, the time, the tool calls and, after each scene, SUCCESSFUL / NOT SUCCESSFUL
with the completion time) to "outputs\\videos\\duck1", plus a small .txt that says where each
scene starts in the video and how it ended.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import math
import os
import time

from agent_harness import experiment
from duck1 import eye_tools
from agent_harness import harness
from duck1 import scenes
from sim.duck_sim import Duck, ViewerClosed
from agent_harness.experiment import ExpLogger, write_meta, summarize
from sim.eye import Eye, OracleEye
from agent_harness.franky_agent import OllamaBrain
from sim.recorder import Recorder
from agent_harness.run_harness import file_hash
from project_paths import log_dir, video_dir


class CaptionBrain:
    """Wraps a brain. With a window open it writes the scene, the last tool result and the tool Franky just chose at the
    bottom of the MuJoCo window. Without a window it does nothing. The answers of the wrapped brain are not changed."""

    def __init__(self, brain, duck):
        self.brain, self.duck = brain, duck
        self.model = brain.model
        self.num_ctx = getattr(brain, "num_ctx", None)
        self.title, self.n = "", 0
        self.first = None                                       # the first tool Franky chose in the current scene
        self.rec = None                                         # the Recorder, if --record is on (its footer shows the same lines)

    def warmup(self):
        return self.brain.warmup()

    def show(self, *lines):
        if getattr(self.duck, "_fast", False):                  # only the fast window has a caption
            self.duck.viewer.caption = "\n".join(l for l in lines if l)
        if self.rec is not None:                                # the video gets the same text
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--perception", choices=["camera", "oracle"], default="camera",
                    help="camera = the duck sees only through its head camera; oracle = perfect eyes (for comparison)")
    ap.add_argument("--scenes", type=int, nargs="*", default=None, help="scene numbers (default: all)")
    ap.add_argument("--max-steps", type=int, default=16)
    ap.add_argument("--guard", choices=["on", "off"], default="off")
    ap.add_argument("--kick-after", type=int, default=0)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true")
    ap.add_argument("--record", action="store_true", help="save the MuJoCo footage of the run as an mp4 (see --video-dir)")
    ap.add_argument("--rec-fps", type=int, default=30, help="frames per second of the saved video (default 30)")
    ap.add_argument("--video-dir", default=video_dir("duck1"))
    ap.add_argument("--logdir", default=log_dir("duck1"))
    args = ap.parse_args()
    args.panel = "off"                                          # (write_meta wants it)
    ids = list(range(len(scenes.SCENES))) if args.scenes is None else args.scenes
    bad = [i for i in ids if not 0 <= i < len(scenes.SCENES)]
    if bad:
        raise SystemExit(f"No such scene: {bad}. Scenes are 0 to {len(scenes.SCENES) - 1}.")

    brain = eye_tools.EyeRuleBrain() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {brain.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")

    # swap in the camera duck's prompt, tools, and referee (harness.py and experiment.py look these names up when they run)
    harness.SYSTEM = experiment.SYSTEM = eye_tools.EYE_SYSTEM
    harness.TOOLS = experiment.TOOLS = eye_tools.EYE_TOOLS
    harness.GuardedTools = eye_tools.EyeTools
    harness.StateTracker = eye_tools.EyeTracker

    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    duck.eye = Eye(duck) if args.perception == "camera" else OracleEye(duck)
    plain_reset = duck.reset

    def reset_scene(seed=0, **kw):                              # harness.run_episode calls duck.reset(seed=<scene number>)
        sc = scenes.SCENES[seed]
        return plain_reset(seed=seed, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
                           banana_xy=sc["banana"], box_xy=sc["box"], banana_yaw=math.radians(sc.get("banana_yaw", 0.0)))

    duck.reset = reset_scene
    cap = CaptionBrain(brain, duck)
    label = (f"{brain.model.replace(':', '-')}_scenes_{'eye' if args.perception == 'camera' else 'oracle'}"
             + ("_guard" if args.guard == "on" else "") + ("_kick" if args.kick_after else ""))
    rec = Recorder(duck, args.video_dir, f"{label}_{time.strftime('%Y%m%d_%H%M%S')}", args.rec_fps) if args.record else None
    cap.rec = rec
    log = ExpLogger(args.logdir, label, "off", type(brain).__name__)
    write_meta(log, args, brain, "scenes", ids, "off", [])
    log.write_json("scenes.json", {str(i): scenes.SCENES[i] for i in ids})
    log.write_json("perception.json", {"perception": args.perception,
                                       "code_hashes": {n: file_hash(n) for n in ("eye.py", "eye_tools.py", "scene_benchmark.py", "scenes.py")}})
    results, firsts = [], {}
    try:
        for ep, sid in enumerate(ids, 1):
            sc = scenes.SCENES[sid]
            print(f"\n=== scene {sid}: {sc['name']}   banana {sc['banana']}  box {sc['box']}  ({sc['note']}) ===")
            cap.title, cap.n = f"SCENE {sid} of {len(scenes.SCENES) - 1}: {sc['name']}", 0
            if rec:
                rec.mark(f"scene {sid}: {sc['name']}")
            cap.show(cap.title)
            r = harness.run_episode(duck, cap, sid, log, ep, args.max_steps, False, args.kick_after, args.guard == "on")
            results.append((sid, r))
            firsts[sid] = cap.first
            print(f"  RESULT: banana in box = {r['success']}   ({r['steps']} tool calls, {r['failed_effects']} missed goals, "
                  f"ended: {r['ended']}, LLM {r['llm_s']} s, sim {r['sim_s']} s)")
            if rec:                                             # footer of the video: SUCCESSFUL / NOT SUCCESSFUL, completion time, tool calls
                rec.set_verdict(r["success"], r["sim_s"], r["steps"], r["llm_s"], r["ended"])
            if args.view:                                       # keep the last picture + the verdict on screen for a moment
                cap.show(cap.title, ("SUCCESSFUL" if r["success"] else "NOT SUCCESSFUL")
                         + f"   {r['sim_s']} s, {r['steps']} tool calls, ended: {r['ended']}")
            if args.view or rec:                                # (the video shows the verdict in its footer during this moment)
                duck.hold(3.0)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    finally:
        summ = summarize(log, [r for _, r in results]) if results else None
        log.close()
        if rec:
            rec.close()
        duck.eye.close()
        duck.close()

    print("\n" + "=" * 86)
    print(f"perception: {args.perception}")
    print(f"{'scene':<7}{'name':<16}{'solved':<9}{'steps':>6}{'missed':>8}  {'first move':<12}ended")
    for sid, r in results:
        print(f"{sid:<7}{scenes.SCENES[sid]['name']:<16}{'YES' if r['success'] else 'NO':<9}{r['steps']:>6}{r['failed_effects']:>8}  {str(firsts.get(sid)):<12}{r['ended']}")
    if summ:
        print("-" * 86)
        print(f"solved {summ['successes']}/{summ['episodes']}   mean steps {summ['mean_steps']}   missed goals {summ['missed_goals']}   "
              f"LLM {summ['mean_llm_s']} s   sim {summ['mean_sim_s']} s per scene   "
              f"first move was observe in {sum(v == 'observe' for v in firsts.values())}/{len(firsts)}")
    print("=" * 86)


if __name__ == "__main__":
    main()