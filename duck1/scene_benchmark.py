"""scene_benchmark.py (v4, AUTONOMOUS Franky) - the 10 fixed scenes of scenes.py, one LLM agent, one line of verdict per scene.

    python duck1/scene_benchmark.py                                       # Franky (gemma4:e2b) on all 10 scenes, headless
    python duck1/scene_benchmark.py --view                                # all scenes WITH the MuJoCo window (real time)
    python duck1/scene_benchmark.py --scenes 0 5 --view                   # only scenes 0 and 5
    python duck1/scene_benchmark.py --command "pick this and go and place"   # a loose command: the duck has to work out what it means
    python duck1/scene_benchmark.py --think off                           # no reasoning mode (much faster, usually worse)
    python duck1/scene_benchmark.py --kick-after 2                        # the banana is knocked away after the 2nd move (the duck is not told)
    python duck1/scene_benchmark.py --guard off                           # no behaviour guard (see below); the safety layer of the body stays on
    python duck1/scene_benchmark.py --pad-square on                       # (only if pointing_check says so) pictures on a square canvas
    python duck1/scene_benchmark.py --find off                            # no `find` tool: the duck has to point at pixels with `measure` only
    python duck1/scene_benchmark.py --record                              # SAVE the MuJoCo footage as an mp4 (no window)

To let the duck LEARN a scene over several attempts (it writes its own notebook between attempts), use duck1/learn.py - it shares everything below.

WHAT THE DUCK IS (see duck1/franky_tools.py): a vision-language model (Gemma 4) with eight tools: look, find, measure, walk, turn, close_beak, open_beak, done (turn: version 12, --turn-tool off removes it).
  * it gets the command, the camera facts, the body facts and the tool descriptions - NO recipe, no list of steps
  * it sees the REAL camera picture (a JPEG goes to the model); no colour detection tells it what it sees
  * it names a thing (`find`: Gemma draws a box, in its native detection mode) or points at pixels (`measure`); the tool answers with raw
    distance / bearing / angle / footprint; it decides every move
  * there is NO rule brain any more. Nothing in this repository can solve a scene without the LLM.

This script only sets the scenes up, runs agent_harness/agent_loop.py once per scene, and prints a table. The TRUE simulator state is read
by the referee (agent_harness/referee.py) after every tool call - for the log and for the success check, never shown to the duck.

Every run writes to  outputs\\logs\\duck1\\run_<time>_<label>\\ :
    transcript.md     what the duck THOUGHT, which tool it called, what came back, what the referee found (read this first)
    frames\\           every picture the duck was given (JPEG), named s<scene>_<step>_<tool>.jpg
    steps.csv         one row per tool call (open in Excel)         episodes.csv  one row per scene
    events.jsonl      everything, in order (no pictures inside)     run_meta.json  prompt, tools, model settings, code hashes
--record saves the MuJoCo footage (recorder.py) to "outputs\\videos\\duck1", plus a .txt with where each scene starts.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import json
import math
import platform
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import mujoco
import numpy as np
import onnxruntime

from agent_harness.agent_loop import AgentLogger, run_episode, MAX_FAILED_GOALS, MAX_SIM_S, MAX_DONE_REJECTS, STALL_WARN, STALL_STOP
from agent_harness.gemma_brain import GemmaBrain
from agent_harness.referee import ON_OBJECT_CM, Referee
from agent_harness.run_harness import file_hash, ollama_info, truth
from duck1 import scenes
from duck1.franky_tools import (BOX_ORDERS, DEFAULT_COMMAND, DEFAULT_WINDOW, FrankyTools, SYSTEM_PROMPT, grasp_window, opening_message, set_grasp_window,
                                 stated_window, texts_for, tools_for, window_words)
from sim.duck_sim import Duck, ViewerClosed
from sim.head_camera import HeadCamera
from sim.recorder import Recorder
from project_paths import log_dir, video_dir


class CaptionBrain:
    """Wraps a brain. With a window open (or --record) it writes the scene, the last tool result and the tool Franky just chose at
    the bottom of the MuJoCo window / the video. Everything else is passed straight to the wrapped brain; its answers are not changed."""

    def __init__(self, brain, duck):
        self.brain, self.duck = brain, duck
        self.title, self.n = "", 0
        self.rec = None                                         # the Recorder, if --record is on (its footer shows the same lines)

    def __getattr__(self, name):                                # model, num_ctx, new_episode, describe, ... come from the real brain
        return getattr(self.brain, name)

    def show(self, *lines):
        try:
            if getattr(self.duck, "_fast", False):              # only the fast window has a caption
                self.duck.viewer.caption = "\n".join(l for l in lines if l)
            if self.rec is not None:                            # the video gets the same text
                self.rec.set_caption([l for l in lines if l], self.n)
        except Exception:                                       # a caption must never stop a run
            pass

    def chat(self, msgs, tools=None, **kw):
        self.n += 1
        last = next((m["content"].splitlines()[0][:110] for m in reversed(msgs) if m["role"] == "tool" and m.get("content")), "")
        self.show(self.title, f"step {self.n}: Franky is thinking...", "last result: " + last if last else "")
        resp = self.brain.chat(msgs, tools, **kw)
        try:
            calls = resp.get("message", {}).get("tool_calls") or []
            if calls:
                fn = calls[0]["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                vals = ", ".join(f"{v}" for k, v in args.items() if k != "note")
                act = f"{fn.get('name')}({vals})" if vals else str(fn.get("name"))
                self.show(self.title, f"step {self.n}: {act}", "last result: " + last if last else "")
        except Exception:
            pass
        return resp


def write_meta(log, args, brain, ids):
    log.write_json("run_meta.json", {
        "run_id": log.run_id, "started": datetime.now().isoformat(timespec="seconds"), "command_line": " ".join(sys.argv),
        "agent": "autonomous Franky: look / find / measure / walk / turn / close_beak / open_beak / done, real camera pictures, no recipe, no rule brain",
        "find": args.find, "box_order": args.box_order, "pad_square": args.pad_square,
        "task_command": args.command, "scenes": ids, "max_steps": args.max_steps, "guard": args.guard, "kick_after": args.kick_after,
        "guard_limits": {"max_failed_steps": MAX_FAILED_GOALS, "max_sim_s": MAX_SIM_S, "max_done_rejects": MAX_DONE_REJECTS,
                         "stall_warn_after": STALL_WARN, "stall_stop_after": STALL_STOP},
        "brain": type(brain).__name__, "llm": brain.describe(),
        "ollama": ollama_info(args.host, args.model),
        "software": {"python": sys.version.split()[0], "platform": platform.platform(), "mujoco": mujoco.__version__,
                     "onnxruntime": onnxruntime.__version__, "numpy": np.__version__},
        "code_hashes": {n: file_hash(n) for n in ("franky_tools.py", "gemma_brain.py", "agent_loop.py", "referee.py", "motion.py",
                                                   "head_camera.py", "duck_sim.py", "guardrails.py", "scene_benchmark.py", "scenes.py")},
        "system_prompt": SYSTEM_PROMPT, "opening_message": opening_message(args.command), "tools": tools_for(args.find == "on", args.turn_tool == "on"), "turn_tool": args.turn_tool})


def build_parser(description="Autonomous Franky on the 10 fixed scenes."):
    """All the options that describe the duck and its world (the benchmark and learn.py share them)."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--model", default="gemma4:e2b", help="any Ollama model with vision + tools (e.g. gemma4:e2b, gemma4:e4b)")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--command", default=DEFAULT_COMMAND, help="what the person says to the duck (it is the only task description the duck gets)")
    ap.add_argument("--scenes", type=int, nargs="*", default=None, help="scene numbers (default: the 10 benchmark scenes 0-9; 10 and 11 are practice scenes)")
    ap.add_argument("--max-steps", type=int, default=40, help="tool calls per scene before the episode is stopped (default 40)")
    ap.add_argument("--think", choices=["on", "off"], default="on", help="Gemma's reasoning mode (default on)")
    ap.add_argument("--num-ctx", type=int, default=8192, help="context window (tokens). Pictures are big; older steps are summarised to fit")
    ap.add_argument("--num-predict", type=int, default=2048, help="max tokens the model may write per turn (thinking + the tool call)")
    ap.add_argument("--keep-images", type=int, default=1, help="how many of the latest pictures the model gets (default 1)")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0, help="the LLM's sampling seed")
    ap.add_argument("--guard", choices=["on", "off"], default="on",
                    help="behaviour guard: rejects a false 'done', warns / stops on loops, budget of failed steps and simulated time "
                         "(default on). The safety layer in sim/motion.py (table edge, box rim, banana bumper) is always on.")
    ap.add_argument("--find", choices=["on", "off"], default="on",
                    help="the `find` tool: Gemma outlines a named thing in the picture in its native box format (default on). "
                         "off = the duck can only point at pixels with `measure`")
    ap.add_argument("--box-order", choices=list(BOX_ORDERS), default="yxyx",
                    help="how the vision model is asked to write a box: yxyx = [y1, x1, y2, x2] (Gemma 4's own order, default) or xyxy. "
                         "pointing_check.py tells you which one fits your Ollama")
    ap.add_argument("--pad-square", choices=["on", "off"], default="off",
                    help="send every picture on a square canvas with grey bars above and below (and read x, y over the whole square). "
                         "Only if pointing_check.py shows that Gemma locates things better that way")
    ap.add_argument("--detail", choices=["compact", "full"], default="compact",
                    help="what is printed in the terminal: compact (default) = one line per step with the result in numbers + the duck's note, and only "
                         "the guard / safety events; full = also the vision model's raw answer, the pointing check, the thinking. "
                         "The files in the log folder (transcript.md, steps.csv, events.jsonl, frames) are the same either way")
    ap.add_argument("--beak-sense", choices=["on", "off"], default="on",
                    help="a closed beak that did not take the banana reports where the banana's middle was relative to the beak tip and the "
                         "angle of its long side, if it was within 0.10 m (raw numbers, default on). off = it only says the banana was not taken")
    ap.add_argument("--turn-tool", choices=["on", "off"], default="on",
                    help="the `turn` tool (version 12): turn on the spot to the left or right by a positive number of degrees, and a new picture (default on). "
                         "off = the seven tools of versions 1-11; walk can turn in both cases (positive = left, negative = right)")
    ap.add_argument("--kick-after", type=int, default=0, help="knock the banana away after this many tool calls (0 = never)")
    ap.add_argument("--view", action="store_true")
    ap.add_argument("--record", action="store_true", help="save the MuJoCo footage of the run as an mp4 (see --video-dir)")
    ap.add_argument("--rec-fps", type=int, default=30, help="frames per second of the saved video (default 30)")
    ap.add_argument("--video-dir", default=video_dir("duck1"))
    ap.add_argument("--logdir", default=log_dir("duck1"))
    return ap


def start_brain(args):
    """The LLM client, checked and warmed up (exits with a clear message when Ollama or the model is not there)."""
    brain = GemmaBrain(args.model, args.host, num_ctx=args.num_ctx, think=args.think == "on", num_predict=args.num_predict,
                       temperature=args.temperature, seed=args.seed, keep_images=args.keep_images)
    print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
    try:
        for w in brain.check():
            print(f"  note: {w}", flush=True)
        print(f"  ready after {brain.warmup():.0f} s   (thinking {'ON' if brain.think else 'OFF'}, context {brain.num_ctx} tokens)", flush=True)
    except RuntimeError as e:
        raise SystemExit(f"{e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")
    except Exception as e:
        raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")
    return brain


def build_world(args, brain, label_extra=""):
    """The simulator, the camera, the tools, the referee and the caption wrapper. -> SimpleNamespace(duck, camera, tools, referee, cap, rec, label)"""
    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    plain_reset = duck.reset

    def reset_scene(seed=0, **kw):                              # run_episode calls duck.reset(seed=<scene number>)
        sc = scenes.SCENES[seed]
        set_grasp_window(*grasp_window(sc))                     # practice scenes may have a wider grasp window; every other scene gets the benchmark window back
        out = plain_reset(seed=seed, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
                          banana_xy=sc["banana"], box_xy=sc["box"], banana_yaw=math.radians(sc.get("banana_yaw", 0.0)))
        if sc.get("hold"):                                      # scene 13: the duck begins holding the banana (the banana lies across its beak, in reach: one crouch takes it)
            duck.ground_pick(grasp=True)
            if not duck.holding:
                raise RuntimeError(f"scene {seed} is meant to start with the banana in the beak, but the first grasp did not take it")
        return out

    duck.reset = reset_scene
    camera = HeadCamera(duck)
    camera.pad_square = args.pad_square == "on"
    tools = FrankyTools(duck, camera, grounder=brain.ground if args.find == "on" else None, box_order=args.box_order,
                        beak_sense=args.beak_sense == "on", turn_tool=args.turn_tool == "on")
    referee = Referee(duck, truth)
    cap = CaptionBrain(brain, duck)
    label = (f"{args.model.replace(':', '-')}_scenes_agent" + ("" if brain.think else "_nothink") + ("_noguard" if args.guard == "off" else "")
             + ("_kick" if args.kick_after else "") + ("_nofind" if args.find == "off" else "") + ("_square" if args.pad_square == "on" else "")
             + ("_nosense" if args.beak_sense == "off" else "") + ("_noturn" if args.turn_tool == "off" else "") + label_extra)
    rec = Recorder(duck, args.video_dir, f"{label}_{time.strftime('%Y%m%d_%H%M%S')}", args.rec_fps) if args.record else None
    cap.rec = rec
    return SimpleNamespace(duck=duck, camera=camera, tools=tools, referee=referee, cap=cap, rec=rec, label=label)


def main():
    ap = build_parser()
    args = ap.parse_args()
    ids = list(range(scenes.N_BENCHMARK)) if args.scenes is None else args.scenes
    bad = [i for i in ids if not 0 <= i < len(scenes.SCENES)]
    if bad:
        raise SystemExit(f"No such scene: {bad}. Scenes are 0 to {len(scenes.SCENES) - 1}.")

    brain = start_brain(args)
    w = build_world(args, brain)
    duck, camera, tools, referee, cap, rec = w.duck, w.camera, w.tools, w.referee, w.cap, w.rec
    log = AgentLogger(args.logdir, w.label)
    write_meta(log, args, brain, ids)
    log.write_json("scenes.json", {str(i): scenes.SCENES[i] for i in ids})
    print(f"logs: {log.dir}", flush=True)
    results = []
    try:
        for ep, sid in enumerate(ids, 1):
            sc = scenes.SCENES[sid]
            print(f"\n=== scene {sid}: {sc['name']}   banana {sc['banana']}  box {sc['box']}  ({sc['note']}) ===")
            if grasp_window(sc) != DEFAULT_WINDOW:
                print(f"    grasp window in this scene: {window_words(stated_window(sc))} as told to the duck, simulator radius {grasp_window(sc)[0] * 100:.0f} cm "
                      f"(the benchmark scenes use {window_words(DEFAULT_WINDOW)})")
            cap.title, cap.n = f"SCENE {sid}: {sc['name']}", 0
            if rec:
                rec.mark(f"scene {sid}: {sc['name']}")
            cap.show(cap.title)
            r = run_episode(duck, cap, tools, referee, log, ep, sid, args.command, *texts_for(sc, args.find == "on", args.turn_tool == "on"), opening_message,
                            max_steps=args.max_steps, guard_on=args.guard == "on", kick_after=args.kick_after, detail=args.detail)
            results.append(r)
            print(f"  RESULT: banana in box = {r['success']}   ({r['steps']} tool calls, {r['failed_effects']} missed goals, "
                  f"{r['refusals']} refused / {r['stops']} stopped by the safety layer, ended: {r['ended']}, "
                  f"LLM {r['llm_s']} s, sim {r['sim_s']} s)")
            if rec:                                             # footer of the video: SUCCESSFUL / NOT SUCCESSFUL, completion time, tool calls
                rec.set_verdict(r["success"], r["sim_s"], r["steps"], r["llm_s"], r["ended"])
            if args.view:                                       # keep the last picture + the verdict on screen for a moment
                cap.show(cap.title, ("SUCCESSFUL" if r["success"] else "NOT SUCCESSFUL")
                         + f"   {r['sim_s']} s, {r['steps']} tool calls, ended: {r['ended']}")
            if args.view or rec:                                # (the video shows the verdict in its footer during this moment)
                duck.hold(3.0)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    except KeyboardInterrupt:
        print("\nstopped with Ctrl+C - the scenes finished so far are summarised below.")
    finally:
        summ = summarize(log, results, args) if results else None
        log.close()
        if rec:
            rec.close()
        camera.close()
        duck.close()

    print("\n" + "=" * 100)
    print(f"model {args.model}   thinking {'on' if brain.think else 'off'}   guard {args.guard}   command: {args.command!r}")
    print(f"{'scene':<7}{'name':<16}{'solved':<8}{'steps':>6}{'missed':>8}{'safety':>8}{'points':>9}  {'first tool':<11}{'ended':<14}{'LLM s':>7}{'sim s':>7}")
    for r in results:
        pts = f"{r['measures_on_object']}/{r['measures']}"
        print(f"{r['scene']:<7}{scenes.SCENES[r['scene']]['name']:<16}{'YES' if r['success'] else 'NO':<8}{r['steps']:>6}{r['failed_effects']:>8}"
              f"{r['refusals'] + r['stops']:>8}{pts:>9}  {str(r['first_tool']):<11}{r['ended']:<14}{r['llm_s']:>7}{r['sim_s']:>7}")
    if summ:
        print("-" * 100)
        print(f"solved {summ['successes']}/{summ['episodes']}   false 'done' claims {summ['false_claims']}   mean tool calls {summ['mean_steps']}   "
              f"missed goals {summ['missed_goals']}   safety refusals/stops {summ['safety_events']}")
        print(f"pointing: {summ['measures_on_object']}/{summ['measures']} measured spots / found-box middles were on the banana (within {ON_OBJECT_CM:.0f} cm) or on the box   "
              f"LLM {summ['mean_llm_s']} s and sim {summ['mean_sim_s']} s per scene   first tool was `look` in {summ['first_look']}/{summ['episodes']}")
        print(f"('points' = measured spots that landed on the true banana (within {ON_OBJECT_CM:.0f} cm) or on the box; 'safety' = moves the safety layer refused or stopped)")
    print("=" * 100)
    print(f"read the transcript: {log.dir}")


def summarize(log, results, args):
    n = len(results)
    summ = {"run_id": log.run_id, "model": args.model, "command": args.command, "episodes": n,
            "successes": sum(r["success"] for r in results), "success_rate": round(sum(r["success"] for r in results) / n, 3),
            "false_claims": sum(r["false_claim"] for r in results),
            "missed_goals": sum(r["failed_effects"] for r in results),
            "loop_flags": sum(r["loop_flags"] for r in results),
            "unexpected_events": sum(r["unexpected_events"] for r in results),
            "safety_events": sum(r["refusals"] + r["stops"] for r in results),
            "guard_actions": sum(r["guard_actions"] for r in results),
            "measures": sum(r["measures"] for r in results), "measures_on_object": sum(r["measures_on_object"] for r in results),
            "finds": sum(r["finds"] for r in results),
            "first_look": sum(r["first_tool"] == "look" for r in results),
            "mean_steps": round(sum(r["steps"] for r in results) / n, 2),
            "mean_llm_s": round(sum(r["llm_s"] for r in results) / n, 1),
            "mean_sim_s": round(sum(r["sim_s"] for r in results) / n, 1),
            "per_scene": {str(r["scene"]): {"success": r["success"], "steps": r["steps"], "ended": r["ended"]} for r in results}}
    log.write_json("run_summary.json", summ)
    return summ


if __name__ == "__main__":
    main()
