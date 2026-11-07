"""referee.py - the referee for autonomous Franky: reads the TRUE simulator state after every tool call.

The duck never sees any of this. It is the same idea as state_tracker.py, rewritten for the new tool set (look, measure, walk,
close_beak, open_beak, done) - the old tracker judged walk_to / "IN REACH" and cannot be reused for low-level tools.

For every tool call it answers:
  1. expected vs actual   did the action really do what it is for?  (effect_ok)   - judged on the true state, not on the tool's text
  2. what changed         did anything happen that the action does not explain?    (unexpected)
  3. is Franky stuck      the same action failing again and again, or nothing changing for several moves   (loop)
  4. POINTING (new)       for `measure` and `find`: where on the table did the model point (or where is the middle of the box it
                          drew), and how far is that from the TRUE banana / box? This is the number that tells you how well the
                          vision model grounds objects in the picture.
Perception calls (look, find, measure) never count as "nothing changed": looking is not a stall.
"""

import json
import math

from agent_harness.state_tracker import StateTracker, _moved
from sim.duck_sim import TABLE_H

STALL_CALLS = 3          # this many moves in a row (walk / pick / place) that change nothing = stalled
PERCEPTION = ("look", "find", "measure")
CANON = {"close_beak": "pick", "open_beak": "place", "turn": "walk"}      # the model's tool names -> the names the tracker's rules use
BANANA_HALF = 0.037      # m: half the banana's length, tip to middle (capsule 0.028 + end cap 0.009)
BOX_HALF = 0.08          # m: the green box's footprint, inner half size 0.07 + its rim
ON_OBJECT_CM = 3.0       # a measured spot counts as "on the object" when it is within this many cm of the banana / on the box
WALK_TOL_M, WALK_TOL_FRAC = 0.05, 0.25      # a walk "worked" if it moved within max(5 cm, 25 %) of what was asked ...
TURN_TOL_DEG, TURN_TOL_FRAC = 15.0, 0.25    # ... and turned within max(15 deg, 25 %)


class Referee:
    def __init__(self, duck, truth_fn):
        self.duck, self.truth_fn = duck, truth_fn
        self.start()

    def start(self):
        """Call at the beginning of every episode."""
        self.now = self.truth_fn(self.duck)
        self.sigs = [StateTracker._sig(self.now)]
        self.streak_key, self.streak = None, 0
        self.counts = {"failed_effects": 0, "loop_flags": 0, "unexpected_events": 0, "refusals": 0, "stops": 0,
                       "measures": 0, "measures_on_object": 0, "finds": 0}
        self.budget_failures = 0            # what the failure budget counts: failed MOVES since the last grasp (see after_call)
        return self.now

    # ------------------------------------------------------------------ pointing accuracy
    def spot_error(self, target, w):
        """Distance in cm from a world point `w` (x, y) to the BODY of the true `target` ("banana" or "box"): 0 = on it. None if there is no such object
        (the banana is held in the beak or lies off the table)."""
        d = self.duck
        if target == "box":
            g = d.box_pos()
            return 100 * math.hypot(max(abs(w[0] - g[0]) - BOX_HALF, 0.0), max(abs(w[1] - g[1]) - BOX_HALF, 0.0))
        b = d.banana_pos()
        if d.holding or b[2] < TABLE_H - 0.05:
            return None
        ax = d.banana_axis()
        ux, uy = math.cos(ax), math.sin(ax)
        t = max(-BANANA_HALF, min(BANANA_HALF, (w[0] - b[0]) * ux + (w[1] - b[1]) * uy))          # nearest point on the banana's axis
        return 100 * math.hypot(w[0] - (b[0] + t * ux), w[1] - (b[1] + t * uy))

    def _pointing(self, meta):
        """For each measured spot (or found box middle): the nearest TRUE object (banana or box) and how far the spot is from its BODY,
        in cm (0 = on the banana / inside the box). For `find` the words the model asked for decide which object is meant."""
        out = []
        hint = str(meta.get("find") or "").lower()
        names = (("banana",) if "banana" in hint else ("box",) if any(w in hint for w in ("box", "tray", "container")) else ("box", "banana"))
        for i, s in enumerate(meta.get("spots", []), 1):
            w = s.get("world")
            if w is None:
                out.append({"spot": i, "nearest": None, "err_cm": None})
                continue
            cands = [(name, self.spot_error(name, w)) for name in names]
            cands = [c for c in cands if c[1] is not None]
            if not cands:                                         # e.g. asked for the banana while it is held in the beak
                out.append({"spot": i, "nearest": None, "err_cm": None})
                continue
            name, err = min(cands, key=lambda c: c[1])
            out.append({"spot": i, "nearest": name, "err_cm": round(err, 1)})
        return out

    # ------------------------------------------------------------------ the verdict on one tool call
    def _effect(self, tool, args, meta, b, a):
        """-> (expected, actual, effect_ok)"""
        if a["fallen"]:
            return "stay upright", "fell over", False
        if a["off_table"]:
            return "stay on the table", "walked off the table", False
        if tool == "look":
            return "a picture", "picture taken", True
        if tool == "measure":
            spots = meta.get("spots", [])
            ok = bool(spots) and all(s.get("world") is not None for s in spots)
            return "a measurement of every spot", ("measured" if ok else "a spot could not be measured"), ok
        if tool == "find":
            spots = meta.get("spots", [])
            ok = any(s.get("world") is not None for s in spots)
            return "a box around what was asked for", ("a box was found" if ok else "no usable box"), ok
        if tool == "walk":
            fwd, trn = meta.get("asked", (0.0, 0.0))
            mv, tn = meta.get("did", (0.0, 0.0))
            if meta.get("refused"):
                return f"walk {fwd:+.2f} m, turn {trn:+.0f}", "refused by the safety layer", False
            if meta.get("stopped"):
                return f"walk {fwd:+.2f} m, turn {trn:+.0f}", f"stopped early: {meta['stopped']}", False
            ok = (abs(mv - fwd) <= max(WALK_TOL_M, WALK_TOL_FRAC * abs(fwd)) and
                  abs(tn - trn) <= max(TURN_TOL_DEG, TURN_TOL_FRAC * abs(trn)))
            return f"walk {fwd:+.2f} m, turn {trn:+.0f}", f"moved {mv:+.2f} m, turned {tn:+.0f}", ok
        if tool == "pick":
            if b["holding"]:
                return "grab the banana", "you were already holding it", False
            return "grab the banana", "banana in the beak" if a["holding"] else "beak is empty", a["holding"]
        if tool == "place":
            if not b["holding"]:
                return "banana in the green box", "you were not holding the banana", False
            dist = math.hypot(a["banana_x"] - a["box_x"], a["banana_y"] - a["box_y"])
            return ("banana in the green box",
                    "banana in the green box" if a["banana_in_box"] else f"banana landed {dist:.2f} m from the box centre",
                    a["banana_in_box"] and not a["holding"])
        if tool == "done":
            return ("banana in the green box",
                    "banana in the green box" if a["banana_in_box"] else "banana is NOT in the green box (false claim)",
                    a["banana_in_box"])
        return "a valid tool", f"unknown tool {tool!r}", False

    def after_call(self, tool, args, meta=None):
        """Judge the tool call that just ended. Returns a dict that goes straight into the log."""
        meta = meta or {}
        b, a = self.now, self.truth_fn(self.duck)
        canon = CANON.get(tool, tool)
        expected, actual, ok = self._effect(canon, args, meta, b, a)
        unexpected = StateTracker._unexpected(canon, b, a)
        shown = {k: v for k, v in (args or {}).items() if k != "note"}
        key = (tool, json.dumps(shown, sort_keys=True))
        self.streak = 0 if ok else (self.streak + 1 if key == self.streak_key else 1)
        self.streak_key = key if not ok else None
        if tool not in PERCEPTION:                                    # looking and measuring are not "moves"
            self.sigs.append(StateTracker._sig(a))
        stalled = len(self.sigs) > STALL_CALLS and len(set(self.sigs[-(STALL_CALLS + 1):])) == 1
        loop = "repeat_fail" if self.streak >= 2 else ("stalled" if stalled and tool not in PERCEPTION else "")
        pointing = self._pointing(meta) if tool in ("measure", "find") else None
        c = self.counts
        c["failed_effects"] += (not ok)
        # The failure budget (Guard) counts failed MOVES (walk, close_beak, open_beak, done, a fall), not failed looking: a find that draws no box
        # or a measure of the grey padding costs a step but nothing else, and a duck that looks more carefully must not be stopped for it.
        # A grasp counts as progress and starts the count again. Looking is still limited: by StallWatch (nothing changes for 10 calls),
        # by the repeat / stalled loop flags, by the step cap and by the simulated-time budget.
        if tool not in PERCEPTION and not ok:
            self.budget_failures += 1
        if canon == "pick" and ok and a["holding"] and not b["holding"]:
            self.budget_failures = 0
        c["loop_flags"] += bool(loop)
        c["unexpected_events"] += bool(unexpected)
        c["finds"] += (tool == "find")
        c["refusals"] += bool(meta.get("refused"))
        c["stops"] += bool(meta.get("stopped"))
        if pointing:
            c["measures"] += len(pointing)
            c["measures_on_object"] += sum(1 for p in pointing if p["err_cm"] is not None and p["err_cm"] <= ON_OBJECT_CM)
        self.now = a
        call = f"{tool}({', '.join(f'{k}={v}' for k, v in shown.items())})"
        return {"call": call, "expected": expected, "actual": actual, "effect_ok": ok, "unexpected": unexpected,
                "changes": StateTracker.diff(b, a), "repeat": self.streak, "loop": loop, "before": b, "after": a,
                "pointing": pointing}
