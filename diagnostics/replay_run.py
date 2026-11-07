"""replay_run.py - replay ONE attempt of a real run exactly as the duck decided it, and save it as a video.

The language model is NOT run. A ReplayBrain gives back, in order, the tool calls (with the duck's own notes) and the answers of the
`find` tool that were written to events.jsonl in the run's log folder. Everything else is the project's own code: the simulator,
the camera, the tools, the referee, the guard and the recorder. The video footer says REPLAY.

Afterwards the replay is compared with the real run, step by step (steps.csv): tool, arguments, result text, and the position of the
duck and of the banana. It says "exact" only if everything matches. If the project's code files are not the ones the run used
(run_meta.json holds their hashes) it says so first, because then an exact copy cannot be expected.

Run it on the computer that made the run (the physics is only repeatable on the same machine and the same software).

    python diagnostics\\replay_run.py --run outputs\\logs\\duck1\\run_20261106_155151_gemma4-e4b_scenes_agent_nothink_square_learn --episode 7 --record
    python diagnostics\\replay_run.py --run <the same folder> --episode 7 --chain --record     # first replay episodes 1-6 too, as the real run did
    python diagnostics\\replay_run.py --run <the same folder> --episode 4 --view --record      # watch it in the MuJoCo window while it is saved

--episode is the number of the attempt in the whole run (learning.csv / episodes.csv: row 7 = the 7th attempt, e.g. scene 12 attempt 3).
It writes a NEW log folder and a NEW video (outputs\\videos\\duck1, name with REPLAY in it); it never changes the run it reads.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # the project folder on the path, so "sim", "agent_harness", "duck1" can be imported

import argparse
import csv
import json
import os
import re
import time

from agent_harness.agent_loop import AgentLogger, run_episode
from agent_harness.run_harness import file_hash
from duck1 import scenes
from duck1.franky_tools import opening_message, texts_for
from duck1.scene_benchmark import build_parser, build_world
from project_paths import ROOT, log_dir, video_dir
from sim.recorder import Recorder

POS = ("duck_x", "duck_y", "duck_yaw_deg", "banana_x", "banana_y", "banana_z")


class ReplayBrain:
    """Gives back what the real model said, in order. Nothing is judged or changed."""

    def __init__(self, events, model, episode):
        self.events, self.model, self.think, self.num_ctx = events, model, False, 8192
        self.ground_whats = []
        self.load(episode)

    def load(self, episode):
        self.msgs = [e["message"] for e in self.events if e["kind"] == "llm_response" and e["episode"] == episode]
        self.grounds = [(e["what"], e["raw"]) for e in self.events if e["kind"] == "grounding" and e["episode"] == episode]
        self.i = self.g = 0

    def new_episode(self):
        pass

    def describe(self):
        return {"model": "REPLAY of the logged answers"}

    def chat(self, messages, tools=None, **kw):
        if self.i >= len(self.msgs):
            raise RuntimeError("the replay ran out of logged answers")
        m = self.msgs[self.i]
        self.i += 1
        return {"message": m, "prompt_eval_count": 0, "eval_count": 0, "done_reason": "stop", "_ctx": {}}

    def ground(self, jpeg, what, order="yxyx"):
        if self.g >= len(self.grounds):
            raise RuntimeError("the replay ran out of logged find answers")
        logged_what, raw = self.grounds[self.g]
        self.g += 1
        self.ground_whats.append((what, logged_what))
        return raw, 0.0


def norm(s):
    return re.sub(r"#\d+", "#N", str(s or "")).strip()               # picture numbers depend on how many pictures were taken before


def read_csv(path):
    return list(csv.DictReader(open(path, encoding="utf-8-sig")))


def main():
    ap = argparse.ArgumentParser(description="Replay one attempt of a real run and save it as a video.")
    ap.add_argument("--run", required=True, help="the run's log folder (outputs\\logs\\duck1\\run_...), absolute or relative to the project folder")
    ap.add_argument("--episode", type=int, required=True, help="which attempt of the run (1 = the first one in episodes.csv)")
    ap.add_argument("--chain", action="store_true", help="first replay the attempts before it, in the same process (the real run did them before)")
    ap.add_argument("--record", action="store_true", help="save the MuJoCo footage as an mp4")
    ap.add_argument("--view", action="store_true", help="watch it in the MuJoCo window")
    ap.add_argument("--video-dir", default=video_dir("duck1"))
    ap.add_argument("--logdir", default=log_dir("duck1"))
    a = ap.parse_args()

    run = a.run if os.path.isabs(a.run) else os.path.join(ROOT, a.run)
    for f in ("events.jsonl", "steps.csv", "episodes.csv", "run_meta.json"):
        if not os.path.exists(os.path.join(run, f)):
            raise SystemExit(f"{f} is missing in {run}")
    meta = json.load(open(os.path.join(run, "run_meta.json"), encoding="utf-8-sig"))
    events = [json.loads(l) for l in open(os.path.join(run, "events.jsonl"), encoding="utf-8-sig")]
    eps = read_csv(os.path.join(run, "episodes.csv"))
    steps = read_csv(os.path.join(run, "steps.csv"))
    if not 1 <= a.episode <= len(eps):
        raise SystemExit(f"--episode must be 1 to {len(eps)} for this run")
    scene_of = {int(r["episode"]): int(r["scene"]) for r in eps}
    llm_of = {int(r["episode"]): float(r["llm_s"] or 0) for r in eps}
    attempt_of = {e: 1 + sum(1 for k in scene_of if k < e and scene_of[k] == scene_of[e]) for e in scene_of}

    # ---- are the code files the ones the run used?
    changed = []
    for name, h in (meta.get("code_hashes") or {}).items():
        now = file_hash(name)
        if now != h:
            changed.append(f"{name} (run {h}, now {now})")
    if changed:
        print("WARNING: these code files are not the ones the run used, so an exact copy cannot be expected:\n   " + "\n   ".join(changed))
    else:
        print("code check: all the files the run listed in run_meta.json are unchanged.")

    model = (meta.get("llm") or {}).get("model", "gemma4:e4b")
    argv = ["--model", model, "--think", "off" if not (meta.get("llm") or {}).get("think") else "on", "--video-dir", a.video_dir, "--logdir", a.logdir,
            "--pad-square", meta.get("pad_square", "off"), "--find", meta.get("find", "on"), "--box-order", meta.get("box_order", "yxyx"),
            "--turn-tool", meta.get("turn_tool", "on"), "--guard", meta.get("guard", "on"), "--max-steps", str(meta.get("max_steps", 40)),
            "--kick-after", str(meta.get("kick_after", 0))]
    if meta.get("task_command"):
        argv += ["--command", meta["task_command"]]
    if a.view:
        argv.append("--view")
    args = build_parser().parse_args(argv)                              # (no --record here: the recorder starts just before the attempt you asked for)

    first = 1 if a.chain else a.episode
    brain = ReplayBrain(events, model, first)
    sid_t, att_t = scene_of[a.episode], attempt_of[a.episode]
    w = build_world(args, brain, label_extra=f"_REPLAY_scene{sid_t}_attempt{att_t}_of_{os.path.basename(run).split('_gemma')[0]}")
    duck, tools, referee, cap = w.duck, w.tools, w.referee, w.cap
    log = AgentLogger(args.logdir, w.label)
    rec = None
    try:
        for ep in range(first, a.episode + 1):
            sid, att = scene_of[ep], attempt_of[ep]
            sc = scenes.SCENES[sid]
            brain.load(ep)
            target = ep == a.episode
            if target and a.record:
                rec = Recorder(duck, a.video_dir, f"{w.label}_{time.strftime('%Y%m%d_%H%M%S')}", args.rec_fps)
                cap.rec = rec
                rec.mark(f"REPLAY scene {sid} attempt {att} (real run {os.path.basename(run)[:19]})")
            cap.title, cap.n = f"REPLAY of the real run - scene {sid}, attempt {att}", 0
            cap.show(cap.title)
            r = run_episode(duck, cap, tools, referee, log, ep, sid, args.command, *texts_for(sc, args.find == "on", args.turn_tool == "on"), opening_message,
                            max_steps=args.max_steps, guard_on=args.guard == "on", kick_after=args.kick_after, tag=f"s{sid:02d}_a{att:02d}",
                            title=f"REPLAY scene {sid} attempt {att}", detail="compact" if target else "full", verbose=target)
            print(f"  episode {ep} (scene {sid} attempt {att}): banana in box = {r['success']}   {r['steps']} tool calls, ended: {r['ended']}")
            if target and rec:
                rec.set_verdict(r["success"], r["sim_s"], r["steps"], llm_of[ep], r["ended"])      # the LLM time shown is the REAL run's
            if args.view or rec:
                duck.hold(3.0)
    finally:
        if rec:
            rec.close()
        log.close()
        w.camera.close()
        duck.close()

    # ---- compare the replay with the real run (the attempt you asked for)
    orig = [r for r in steps if r["episode"] == str(a.episode)]
    new = [r for r in read_csv(os.path.join(log.dir, "steps.csv")) if r["episode"] == str(a.episode)]
    bad, first_diff, maxd = 0, None, {k: 0.0 for k in POS}
    for o, n in zip(orig, new):
        same = (o["tool"] == n["tool"] and o["args"] == n["args"] and norm(o["result"]) == norm(n["result"])
                and (o["holding_after"], o["banana_in_box"]) == (n["holding_after"], n["banana_in_box"]))
        for k in POS:
            try:
                maxd[k] = max(maxd[k], abs(float(o[k]) - float(n[k])))
            except ValueError:
                pass
        if not same:
            bad += 1
            first_diff = first_diff or o["step"]
    asked_same = all(x == y for x, y in brain.ground_whats)
    print("\n" + "=" * 90)
    print(f"REAL RUN vs REPLAY, attempt {a.episode} (scene {sid_t}, attempt {att_t}):   {len(orig)} steps in the run, {len(new)} in the replay")
    print(f"  steps whose tool, arguments, result text or holding / in-box state differ: {bad}" + (f"   (first at step {first_diff})" if bad else ""))
    print("  largest difference in position (m, deg): " + ", ".join(f"{k} {v:.4f}" for k, v in maxd.items()))
    print(f"  the replay asked `find` the same questions as the run: {asked_same}")
    exact = bad == 0 and len(orig) == len(new) and max(v for k, v in maxd.items() if k != "duck_yaw_deg") < 0.001 and maxd["duck_yaw_deg"] < 0.1
    print("  RESULT: " + ("EXACT - the replay is the same attempt as the real run." if exact else
                          "NOT exact - the replay made the same decisions, but the physics gave slightly different movements."))
    if a.record:
        print("  video folder:", a.video_dir)


if __name__ == "__main__":
    main()
