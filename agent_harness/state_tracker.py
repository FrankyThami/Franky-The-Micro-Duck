"""state_tracker.py - Harness v1, part A2: Franky's "state situation".

After EVERY tool call it answers three questions from the TRUE simulator state (not from the tool's text):
  1. expected vs actual - did the action really achieve what it is for?   (effect_ok)
  2. what changed       - did anything happen that the action does not explain?   (unexpected)
  3. is Franky stuck    - the same action failing again and again, or nothing changing at all?   (loop)
And BEFORE every LLM decision it asks a fourth:
  4. scene change       - did the world change on its own since the last action?   (external)

It also writes the short "[status]" text the LLM may see (only when the harness runs with --panel on).
"""

import json
import math

from sim.duck_sim import TABLE_H

MOVE_EPS = 0.02      # m   : a banana / duck / box move smaller than this counts as noise
YAW_EPS = 10.0       # deg : a turn smaller than this counts as noise
STALL_CALLS = 3      # this many calls in a row with NO change in the world = stalled


def _moved(a, b, name):
    return math.hypot(a[name + "_x"] - b[name + "_x"], a[name + "_y"] - b[name + "_y"])


class StateTracker:
    def __init__(self, duck, tools, truth_fn):
        self.duck, self.tools, self.truth_fn = duck, tools, truth_fn
        self.start()

    # ------------------------------------------------------------------ snapshots
    def snapshot(self):
        """True state (from truth_fn) + the two 'in reach' flags, using the SAME definition the tools use."""
        d, t = self.duck, self.tools
        s = self.truth_fn(d)
        on_table = s["banana_z"] > TABLE_H - 0.05
        s["banana_reach"] = bool(on_table and not s["holding"] and not s["banana_in_box"]
                                 and t._in_reach(*t._rel(d.banana_pos()[:2])))
        s["box_reach"] = bool(t._in_reach(*t._rel(t._drop_spot())))
        return s

    @staticmethod
    def _sig(s):
        """A coarse fingerprint of the world, to notice 'nothing changed'."""
        return (round(s["duck_x"] / MOVE_EPS), round(s["duck_y"] / MOVE_EPS), round(s["duck_yaw_deg"] / YAW_EPS),
                s["holding"], round(s["banana_x"] / MOVE_EPS), round(s["banana_y"] / MOVE_EPS))

    def start(self):
        """Call at the beginning of every episode."""
        self.now = self.last_after = self.snapshot()
        self.sigs = [self._sig(self.now)]
        self.streak_key, self.streak = None, 0
        self.counts = {"failed_effects": 0, "loop_flags": 0, "unexpected_events": 0, "external_changes": 0}
        return self.now

    # ------------------------------------------------------------------ what changed between two snapshots
    @staticmethod
    def diff(a, b):
        """Plain-language list of everything that differs between snapshot a and snapshot b."""
        out = []
        if a["holding"] != b["holding"]:
            out.append("you are now holding the banana" if b["holding"] else "you no longer hold the banana")
        if a["banana_in_box"] != b["banana_in_box"]:
            out.append("the banana is now in the green box" if b["banana_in_box"] else "the banana is no longer in the green box")
        if not a["holding"] and not b["holding"] and _moved(a, b, "banana") > MOVE_EPS:
            out.append(f"the banana moved {_moved(a, b, 'banana'):.2f} m")
        if _moved(a, b, "box") > MOVE_EPS:
            out.append(f"the green box moved {_moved(a, b, 'box'):.2f} m")
        if _moved(a, b, "duck") > MOVE_EPS:
            out.append(f"you moved {_moved(a, b, 'duck'):.2f} m")
        turn = abs((b["duck_yaw_deg"] - a["duck_yaw_deg"] + 180) % 360 - 180)
        if turn > YAW_EPS:
            out.append(f"you turned {turn:.0f} deg")
        if b["fallen"] and not a["fallen"]:
            out.append("you fell over")
        if b["off_table"] and not a["off_table"]:
            out.append("you walked off the table")
        return out

    # ------------------------------------------------------------------ BEFORE the LLM decides
    def sense(self):
        """Did the world change by itself since the last tool call ended? -> list of changes (usually empty)."""
        self.now = self.snapshot()
        ext = self.diff(self.last_after, self.now)
        self.last_after = self.now                  # report each change once
        self.counts["external_changes"] += bool(ext)
        return ext

    def change_message(self, ext):
        """What the LLM is told when the scene changed (a fresh look is included, so no step is wasted)."""
        return ("[status] SCENE CHANGE since your last action: " + "; ".join(ext) +
                ". Your earlier plan may no longer be valid.\nYou look around:\n" + self.tools.observe())

    # ------------------------------------------------------------------ AFTER a tool call
    def _where(self, which):
        return self.tools._banana_line() if which == "banana" else self.tools._box_line()

    def _effect(self, tool, args, b, a):
        """-> (expected, actual, effect_ok): did the tool achieve its purpose, judged on the true state?"""
        if a["fallen"]:
            return "stay upright", "fell over", False
        if a["off_table"]:
            return "stay on the table", "walked off the table", False
        target = (args or {}).get("target")
        if tool == "walk_to" and target == "banana":
            if b["holding"]:
                return "banana in reach", "you already hold the banana (nothing to walk to)", False
            return "banana in reach", self._where("banana"), a["banana_reach"]
        if tool == "walk_to" and target == "green_box":
            dropped = b["holding"] and not a["holding"]
            return ("green box in reach, banana still held",
                    self._where("green_box") + ("; the banana was DROPPED" if dropped else ""),
                    a["box_reach"] and a["holding"] == b["holding"])
        if tool == "walk_to":
            return "a valid target", f"unknown target {target!r}", False
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
        if tool == "observe":
            ch = self.diff(b, a)
            return "nothing changes", "nothing changed" if not ch else "changed: " + "; ".join(ch), not ch
        if tool == "done":
            return ("banana in the green box",
                    "banana in the green box" if a["banana_in_box"] else "banana is NOT in the green box (false claim)",
                    a["banana_in_box"])
        return "a valid tool", f"unknown tool {tool!r}", False

    @staticmethod
    def _unexpected(tool, b, a):
        """Things that happened which this tool does not explain."""
        out = []
        if b["holding"] and not a["holding"] and tool != "place":
            out.append("the banana was dropped")
        if not b["holding"] and a["holding"] and tool != "pick":
            out.append("the banana appeared in the beak")
        if not b["holding"] and not a["holding"] and tool != "place" and _moved(b, a, "banana") > MOVE_EPS:
            out.append(f"the banana was pushed {_moved(b, a, 'banana'):.2f} m")
        if b["banana_in_box"] and not a["banana_in_box"]:
            out.append("the banana left the green box")
        if a["banana_z"] < TABLE_H - 0.05 and b["banana_z"] >= TABLE_H - 0.05:
            out.append("the banana fell off the table")
        if _moved(b, a, "box") > MOVE_EPS:
            out.append("the green box was moved")
        if a["fallen"]:
            out.append("you fell over")
        elif a["tilt_deg"] > 35:
            out.append(f"you wobbled (tilt {a['tilt_deg']:.0f} deg)")
        if a["off_table"]:
            out.append("you walked off the table")
        return out

    def after_call(self, tool, args):
        """Judge the tool call that just ended. Returns a dict that goes straight into the log."""
        b, a = self.now, self.snapshot()
        expected, actual, ok = self._effect(tool, args, b, a)
        unexpected = self._unexpected(tool, b, a)
        key = (tool, json.dumps(args or {}, sort_keys=True))
        self.streak = 0 if ok else (self.streak + 1 if key == self.streak_key else 1)
        self.streak_key = key if not ok else None
        self.sigs.append(self._sig(a))
        stalled = len(self.sigs) > STALL_CALLS and len(set(self.sigs[-(STALL_CALLS + 1):])) == 1
        loop = "repeat_fail" if self.streak >= 2 else ("stalled" if stalled else "")
        self.counts["failed_effects"] += (not ok)
        self.counts["loop_flags"] += bool(loop)
        self.counts["unexpected_events"] += bool(unexpected)
        self.last_after = a
        call = f"{tool}({', '.join(str(v) for v in (args or {}).values())})"
        return {"call": call, "expected": expected, "actual": actual, "effect_ok": ok, "unexpected": unexpected,
                "changes": self.diff(b, a), "repeat": self.streak, "loop": loop, "before": b, "after": a}

    # ------------------------------------------------------------------ the text the LLM may see
    def panel(self, v):
        """Short status text, appended to the tool result when the harness runs with --panel on."""
        hold = "banana" if v["after"]["holding"] else "nothing"
        if v["effect_ok"]:
            last = f"{v['call']} worked ({v['expected']})"
        else:
            last = f"{v['call']} did NOT work (expected: {v['expected']}; got: {v['actual']})"
        lines = [f"[status] you hold: {hold}. last action: {last}."]
        if v["unexpected"]:
            lines.append("[status] unexpected: " + "; ".join(v["unexpected"]) + ".")
        if v["loop"] == "repeat_fail":
            lines.append(f"[status] WARNING: {v['call']} has failed {v['repeat']} times in a row. "
                         "Do not repeat it. Observe, then try a different action.")
        elif v["loop"] == "stalled":
            lines.append(f"[status] WARNING: nothing has changed during the last {STALL_CALLS} actions. Try a different action.")
        return "\n".join(lines)