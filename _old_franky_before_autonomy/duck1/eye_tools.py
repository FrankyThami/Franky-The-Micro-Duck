"""eye_tools.py (v3) - Franky's tools when his ONLY sense is his own head camera (Step 3).

What changes compared with the old tools in franky_agent.py:
  * Nothing is told to the duck. It knows where the banana and the green box are only after it has LOOKED with its
    camera (eye.py), and only if they are in the picture (the camera looks forward and down, ~100 deg wide; it
    cannot see behind the duck, nor the table closer than ~10 cm in front of the beak).
  * The duck's FIRST move is its own `observe`. (The harness asks observe() once to write the opening message; this
    class answers that one call with "you have not looked yet", so no information leaks into the opening message.)
  * Tools for searching: look_around (step back a little, then turn on the spot in 90-degree steps, looking each time,
    until the thing it needs is in view), and the simple turn / step_back.
  * walk_to uses what the duck saw (its memory), not the simulator. For the banana it comes at it from the SIDE, so that
    the banana lies ACROSS the beak (duck_sim.approach_banana): the duck carries the banana crosswise, like a bar across
    its face, which is the stable way to hold it.
  * pick / place only start when the duck BELIEVES the target is in reach; the physics then decides what really happens.

v3: the banana is held CROSSWISE (across the beak), not lengthwise. "In reach" now needs the banana to lie across the
beak, the texts say so, and the prompt tells the duck how it holds the banana.

v2 (after the first gemma4:e2b run): look_around was added; the "not in view" messages now say what to do; walk_to
green_box is refused while the duck holds nothing; the referee no longer calls a 180-degree turn a failure when the
duck turned the other way round (+180 and -180 are the same direction).

What the duck still gets for free (say so in any write-up): its own position and heading (odometry), the camera's
position (from its joint angles), the table height and the banana's size (prior knowledge).
The state tracker / judge (EyeTracker) still reads the TRUE state - it is the referee, the duck never sees it.
"""

import math
import re

from sim.duck_sim import PICK_ANGLE_TOL, TABLE_HALF
from agent_harness.guardrails import EDGE_MARGIN, GuardedTools
from agent_harness.state_tracker import StateTracker, _moved

# =============================================================================== the prompt and the tool list
EYE_SYSTEM = (
    "You are Franky, a small duck robot standing on a table. Your job: pick up the banana and put it into the green box. "
    "You act ONLY by calling one tool at a time, and after each call you see the result.\n"
    "You can only see through the camera on your head. It looks forward and down, about 100 degrees wide. It cannot "
    "see behind you, and it cannot see the table closer than about 10 cm in front of you. You do not know where "
    "anything is until you have looked, so your first action is observe.\n"
    "You hold the banana crosswise in your beak, like a bar across your face, so you can only pick it up when it lies "
    "ACROSS your beak (left to right in front of you), never along it. walk_to the banana lines you up for that.\n"
    "walk_to, turn, step_back, look_around and place all end with a new look, so do not call observe right after them.\n"
    "If the banana (or the green box, when you already hold the banana) is 'not in view', call look_around.\n"
    "Then: walk_to the banana, pick, walk_to the green_box, place. "
    "If a tool fails, read the message and try something else. "
    "Call done only when you see 'banana: on the green_box'.")

EYE_TOOLS = [
    {"type": "function", "function": {
        "name": "observe", "description": "Look through your camera and report what you see and where it is.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "look_around",
        "description": "Search for the banana (or for the green box if you hold the banana): step back a little, then "
                       "turn on the spot in 90-degree steps, looking each time, until you see it. Use it when "
                       "something you need is not in view.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "turn",
        "description": "Turn on the spot to the left or right by the given angle, then look. To search, use "
                       "look_around instead.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["left", "right"]},
            "degrees": {"type": "integer", "description": "how far to turn, from 45 to 180"}},
            "required": ["direction", "degrees"]}}},
    {"type": "function", "function": {
        "name": "step_back",
        "description": "Walk backwards about 10 cm, then look. Use it when something may be hidden right under your beak.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "walk_to",
        "description": "Walk toward an object you have seen until it is within reach of the beak. For the banana "
                       "you also line yourself up so that it lies across your beak. Use it when the object you need is "
                       "NOT yet in reach.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["banana", "green_box"]}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "pick",
        "description": "Crouch and grab the banana crosswise with the beak. Only works when you hold nothing and "
                       "the banana lies across your beak and is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "place",
        "description": "Crouch and let go of the banana you are holding over the green box, then step back and look. "
                       "Only works when you hold the banana and the green box is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "done", "description": "Say you are finished. Only call it when you see 'banana: on the green_box'.",
        "parameters": {"type": "object", "properties": {}}}},
]

OPENING = ("Nothing yet - you have not used your camera, so you do not know where the banana or the green box are. "
           "Call observe first.")

ON_BOX = 0.095           # a banana seen this close (m) to the middle of the green box counts as "on the green_box"
#                        (the tray is 0.07 m from its middle to its walls; +0.025 for what the camera gets wrong: the
#                         tray wall hides part of a banana lying behind it, which pushes its seen middle away)
ALIGN_MARGIN = 3.0       # deg: the duck wants to be this much better aligned than the physics demands
SCAN_STEP = 90.0         # deg: look_around turns this far between two looks
SCAN_MAX = 270.0         # deg: ... and at most this far in total (with the first look that is all the way round)


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# =============================================================================== the tools
class EyeTools(GuardedTools):
    """The same 'skills' as FrankyTools, but every fact the duck uses comes from its camera.
    duck.eye must exist (eye.Eye = the real camera, eye.OracleEye = perfect eyes for comparison).
    The inherited truth-based helpers (_banana_line, _box_line, _drop_spot, _rel, _in_reach) are left alone on purpose:
    the state tracker uses them as the referee. The duck itself only uses the methods in this class."""

    def __init__(self, duck, on=True):
        super().__init__(duck, on)
        self.eye = duck.eye
        self.mem = {"banana": None, "box": None}     # what the duck remembers: world x,y (+ banana direction)
        self._opened = False

    # --------------------------------------------------------------- seeing and remembering
    def _to_world(self, ahead, left):
        x, y, yaw, _ = self.d.pose()
        c, s = math.cos(yaw), math.sin(yaw)
        return (x + c * ahead - s * left, y + s * ahead + c * left)

    @staticmethod
    def _should_be_visible(ahead, left):
        """Is this place inside the part of the table the camera sees well?"""
        return 0.15 <= ahead <= 0.9 and abs(math.degrees(math.atan2(left, ahead))) <= 35

    def _look(self):
        """Take a picture and update the memory with what is in it."""
        d = self.d
        seen = self.eye.look()
        yaw = d.pose()[2]
        b, g = seen["banana"], seen["box"]
        # --- the green box
        if g is not None:
            if self.mem["box"] is None or not g.get("cut"):          # a box cut by the picture edge: keep the better old guess
                self.mem["box"] = {"xy": self._to_world(g["ahead"], g["left"])}
            self.mem["box"]["seen_now"] = True
        elif self.mem["box"] is not None:
            self.mem["box"]["seen_now"] = False
        # --- the banana
        if d.holding:
            self.mem["banana"] = None                                # it is in the beak
        elif b is not None:
            if b.get("angle_deg") is not None:
                axis = yaw + math.radians(b["angle_deg"])
            else:                                                    # seen end-on: its long side lies along the line of sight
                axis = yaw + math.radians(b["bearing_deg"])
            xy = self._to_world(b["ahead"], b["left"])
            on_box = bool(self.mem["box"] and abs(xy[0] - self.mem["box"]["xy"][0]) < ON_BOX
                          and abs(xy[1] - self.mem["box"]["xy"][1]) < ON_BOX)
            self.mem["banana"] = {"xy": xy, "axis": axis, "seen_now": True, "on_box": on_box}
        elif self.mem["banana"] is not None:
            m = self.mem["banana"]
            m["seen_now"] = False
            if self._should_be_visible(*self._rel(m["xy"])):         # I look right at the place and it is not there any more
                self.mem["banana"] = None
        return seen

    # --------------------------------------------------------------- what the duck believes (from memory)
    def _rel_angle(self, axis):
        """Direction of the banana's long side relative to the beak, degrees in [-90, 90)."""
        return (math.degrees(axis - self.d.pose()[2]) + 90.0) % 180.0 - 90.0

    def _across_err(self, axis):
        """How far (0..90 deg) the banana is from lying exactly ACROSS the beak: 0 = perfectly across, 90 = along the beak."""
        return 90.0 - abs(self._rel_angle(axis))

    def _drop_spot_b(self):
        """Where the beak must end up to drop the banana into the tray: 3.5 cm before the tray centre."""
        bx, by = self.mem["box"]["xy"]
        x, y = self.d.pose()[:2]
        n = math.hypot(bx - x, by - y) or 1.0
        return (bx - 0.035 * (bx - x) / n, by - 0.035 * (by - y) / n)

    def _banana_in_reach(self):
        m = self.mem["banana"]
        if m is None or self.d.holding:
            return False
        return bool(self._in_reach(*self._rel(m["xy"])) and self._across_err(m["axis"]) <= PICK_ANGLE_TOL - ALIGN_MARGIN)

    def _box_in_reach(self):
        return bool(self.mem["box"] is not None and self._in_reach(*self._rel(self._drop_spot_b())))

    def _seen_now(self, what):
        """Is `what` ("banana" or "box") in the picture right now?"""
        m = self.mem[what]
        return bool(m is not None and m["seen_now"])

    @staticmethod
    def _where(ahead, left):
        fwd = f"{ahead:.2f} m ahead" if ahead >= 0 else f"{-ahead:.2f} m behind"
        return f"{fwd}, {abs(left):.2f} m {'left' if left >= 0 else 'right'}"

    def _line_banana(self):
        m = self.mem["banana"]
        if self.d.holding:
            return None
        if m is None:
            return "banana: not in view."
        if m["seen_now"] and m["on_box"]:
            return "banana: on the green_box."
        err = self._across_err(m["axis"])
        how = ("across your beak" if err <= 20 else
               ("along your beak" if err >= 70 else f"slanted, {err:.0f} deg away from across your beak"))
        txt = (f"banana: {self._where(*self._rel(m['xy']))}, lying {how} -> "
               f"{'IN REACH' if self._banana_in_reach() else 'NOT in reach'}")
        return txt + ("" if m["seen_now"] else " (remembered, not in view now)")

    def _line_box(self):
        m = self.mem["box"]
        if m is None:
            return "green_box: not in view."
        reach = "IN REACH" if self._box_in_reach() else "NOT in reach"
        txt = f"green_box: {self._where(*self._rel(m['xy']))} -> {reach}"
        return txt + ("" if m["seen_now"] else " (remembered, not in view now)")

    def _hint(self):
        """One line that says what to do when the thing the duck needs next is completely unknown to it."""
        if self.d.holding:
            return "Hint: you do not know where the green box is - use look_around." if self.mem["box"] is None else None
        return "Hint: you do not know where the banana is - use look_around." if self.mem["banana"] is None else None

    def _view(self, first=None, hint=True):
        """The text the duck reads after a look: the banana line, the box line and what it holds. The line named in
        `first` goes first (so the harness's one-line log shows it); without `first` the 'You hold' line goes first."""
        lines = {"banana": self._line_banana(), "green_box": self._line_box()}
        order = ([first] if first else []) + [k for k in ("banana", "green_box") if k != first]
        body = [lines[k] for k in order if lines[k]]
        hold = f"You hold: {'banana' if self.d.holding else 'nothing'}."
        text = "\n".join(body + [hold] if first else [hold] + body)
        h = self._hint() if hint else None
        return text + ("\n" + h if h else "")

    # --------------------------------------------------------------- the tools
    def observe(self):
        d = self.d
        if d.is_fallen():
            return "You have fallen over and cannot move."
        if d.off_table():
            return "You walked off the table."
        if not self._opened:                       # the harness's own call, made once to write the opening message
            self._opened = True
            return OPENING
        self._look()
        return "You look through your camera.\n" + self._view()

    def look_around(self):
        """Search: look; if what is needed is not in view, step back a little (to uncover what is right under the
        beak), then turn left in 90-degree steps, looking each time, until it is in view or the duck is all the way round."""
        d = self.d
        if d.is_fallen():
            return "You have fallen over and cannot move."
        if d.off_table():
            return "You walked off the table."
        need = "box" if d.holding else "banana"
        self._look()
        stepped, turned = 0.0, 0.0
        if not self._seen_now(need):
            stepped = d.back_up(0.10)["moved"]
            if d.is_fallen():
                return "Failed: you fell over."
            if d.off_table():
                return "Failed: you walked off the table."
            self._look()
        while not self._seen_now(need) and turned < SCAN_MAX:
            d.turn_to(d.pose()[2] + math.radians(SCAN_STEP), tol_deg=10.0)
            d.settle()
            if d.is_fallen():
                return "Failed: you fell over."
            turned += SCAN_STEP
            self._look()
        parts = ([f"stepped back {stepped * 100:.0f} cm"] if stepped >= 0.03 else []) + \
                ([f"turned left {turned:.0f} degrees"] if turned else [])
        text = "You looked around" + (" (" + ", then ".join(parts) + ")" if parts else "") + "."
        if not self._seen_now(need):
            text += f" You did not see {'the green box' if need == 'box' else 'the banana'} anywhere around you."
        return text + "\n" + self._view(hint=False)

    def turn(self, direction=None, degrees=90):
        d = self.d
        if direction not in ("left", "right"):
            return "Unknown direction. Use direction = left or right."
        deg = max(45.0, min(180.0, _num(degrees, 90.0)))           # (turning on the spot by a small angle does not work)
        d.turn_to(d.pose()[2] + (1 if direction == "left" else -1) * math.radians(deg), tol_deg=10.0)
        d.settle()
        if d.is_fallen():
            return "Failed: you fell over."
        self._look()
        return f"Turned {direction} about {deg:.0f} degrees. You look again.\n" + self._view()

    def step_back(self):
        d = self.d
        r = d.back_up(0.10)
        if d.is_fallen():
            return "Failed: you fell over."
        if d.off_table():
            return "Failed: you walked off the table."
        if r["moved"] < 0.03:
            return "Failed: you could not step back (the table edge is close behind you)."
        self._look()
        return f"Stepped back {r['moved'] * 100:.0f} cm. You look again.\n" + self._view()

    def walk_to(self, target=None):
        d = self.d
        if target not in ("banana", "green_box"):
            return "Unknown target. Use target = banana or green_box."
        if target == "banana":
            if d.holding:
                return "You are already holding the banana."
            m = self.mem["banana"]
            if m is None:
                return "Cannot walk to the banana: you have not seen it. Use look_around to find it."
            if m["seen_now"] and m["on_box"]:
                return "The banana is already in the green box."
            xy = m["xy"]
        else:
            if not d.holding:
                m = self.mem["banana"]
                if m is not None and m["seen_now"] and m["on_box"]:
                    return "The banana is already in the green box."
                return "Cannot walk to the green_box yet: you are not holding the banana. Find the banana and pick it up first."
            if self.mem["box"] is None:
                return "Cannot walk to the green_box: you have not seen it. Use look_around to find it."
            xy = self._drop_spot_b()
        if self.on and (abs(xy[0]) > TABLE_HALF[0] - EDGE_MARGIN or abs(xy[1]) > TABLE_HALF[1] - EDGE_MARGIN):
            self._trigger("table_edge", f"{target} at ({xy[0]:.2f}, {xy[1]:.2f})")
            return f"Refused for safety: the {target} is too close to the table edge, you could fall off."
        t0 = d.t
        if target == "banana":
            if self._banana_in_reach():
                return "The banana is already within reach of your beak.\n" + self._view("banana")
            x, y = d.pose()[:2]
            if math.hypot(xy[0] - x, xy[1] - y) < 0.20:
                d.back_up(0.12)                                    # too close to line up: back off first
            box = self.mem["box"]["xy"] if self.mem["box"] else None
            d.approach_banana(xy, m["axis"], box_xy=box)
            d.settle()
            if not (d.is_fallen() or d.off_table()):
                self._look()
        else:
            if self._box_in_reach():
                return "The green box is already within reach of your beak.\n" + self._view("green_box")
            for attempt in range(2):                               # the duck drifts when it stops: check, and correct once
                xy = self._drop_spot_b()
                x, y = d.pose()[:2]
                if math.hypot(xy[0] - x, xy[1] - y) < 0.18:
                    d.back_up(0.12)                                # walk_to would stop at once, facing the wrong way
                d.walk_to(xy)
                d.settle()
                if d.is_fallen() or d.off_table():
                    break
                self._look()
                if self._box_in_reach():
                    break
        if d.is_fallen():
            return "Failed: you fell over."
        if d.off_table():
            return "Failed: you walked off the table."
        return f"Walked toward {target} ({d.t - t0:.1f} s).\n" + self._view("banana" if target == "banana" else "green_box")

    def pick(self):
        d = self.d
        if d.holding:
            return "Cannot pick: you already hold the banana."
        if self.mem["banana"] is None:
            return "Cannot pick: you cannot see the banana. Use look_around to find it."
        if not self._banana_in_reach():
            return "Cannot pick: " + self._line_banana() + "\nUse walk_to banana first."
        p = d.ground_pick(grasp=True)
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        if p["grasped"]:
            self.mem["banana"] = None
            return "Picked: the banana is in your beak."
        self._look()
        return "Missed: the beak closed on nothing, the banana is still on the table.\n" + self._view("banana")

    def place(self):
        d = self.d
        if not d.holding:
            return "Cannot place: you are not holding the banana."
        if self.mem["box"] is None:
            return "Cannot place: you cannot see the green box. Use look_around to find it."
        if not self._box_in_reach():
            return "Cannot place: " + self._line_box() + "\nUse walk_to green_box first."
        d.ground_pick(release=True)
        d.hold(1.5)                                  # let the banana settle
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        d.back_up(0.10)                              # step back so the tray is in the picture again
        d.settle()
        self._look()
        return "Released the banana over the green box and stepped back to look.\n" + self._view("banana")

    def call(self, name, args):
        names = ("observe", "look_around", "turn", "step_back", "walk_to", "pick", "place", "done")
        fn = getattr(self, name, None) if name in names else None
        if fn is None:
            return f"Unknown tool '{name}'. Tools: {', '.join(names)}."
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"


# =============================================================================== the referee
class EyeTracker(StateTracker):
    """The state tracker for the camera duck: also judges `look_around`, `turn` and `step_back`, and 'banana in reach'
    now also means 'the banana lies across the beak' (the physics demands it). Still reads the TRUE state."""

    def snapshot(self):
        s = super().snapshot()
        s["beak_err"] = round(self.duck.grasp_err_deg(), 1)
        s["banana_reach"] = bool(s["banana_reach"] and s["beak_err"] <= PICK_ANGLE_TOL)
        return s

    def _effect(self, tool, args, b, a):
        if tool in ("look_around", "turn", "step_back") and not (a["fallen"] or a["off_table"]):
            if tool == "look_around":
                what = "box" if b["holding"] else "banana"
                m = self.tools.mem[what]
                ok = bool(m is not None and m["seen_now"])
                return (f"find the {'green box' if what == 'box' else 'banana'}",
                        "found it" if ok else "did not find it", ok)
            if tool == "turn":
                want = max(45.0, min(180.0, _num((args or {}).get("degrees"), 90.0))) * (1 if (args or {}).get("direction") == "left" else -1)
                got = (a["duck_yaw_deg"] - b["duck_yaw_deg"] + 180) % 360 - 180
                miss = abs((got - want + 180) % 360 - 180)         # +180 and -180 are the same direction
                return f"turn {want:+.0f} deg", f"turned {got:+.0f} deg", bool(miss <= max(25.0, 0.4 * abs(want)))
            moved = _moved(b, a, "duck")
            return "move back about 10 cm", f"moved {moved:.2f} m", moved >= 0.04
        expected, actual, ok = super()._effect(tool, args, b, a)
        if tool == "walk_to" and (args or {}).get("target") == "banana":
            actual += f"; the banana is {a['beak_err']:.0f} deg away from lying across the beak"
        return expected, actual, ok


# =============================================================================== a brain without an LLM
class EyeRuleBrain:
    """NO LLM. A few if-statements that read the same text an LLM would read (first move: observe; if what it needs is
    not in view: look_around). Tests that the tools, the camera and the logging work together - and shows what a
    'perfect brain' scores with these eyes."""

    model = "eye-rules"

    def warmup(self):
        return 0.0

    def chat(self, messages, tools=None, **kw):
        def call(name, **args):
            return {"message": {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                    "prompt_eval_count": 0, "eval_count": 0}

        tool_msgs = [m["content"] for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            return call("observe")
        last = tool_msgs[-1]
        hist = [c["function"]["name"] for m in messages if m["role"] == "assistant" for c in (m.get("tool_calls") or [])]
        holding = False
        for t in tool_msgs:
            if t.startswith("Picked"):
                holding = True
            elif t.startswith("Released"):
                holding = False
        if "You hold: banana" in last:
            holding = True
        banana = (re.search(r"banana: ([^\n]*)", last) or [None, ""])[1]
        box = (re.search(r"green_box: ([^\n]*)", last) or [None, ""])[1]
        if not holding and banana.startswith("on the green_box"):
            return call("done")
        if last.startswith("Picked"):
            return call("walk_to", target="green_box")
        if last.startswith(("Cannot", "Unknown", "Bad arguments", "Failed", "Nothing yet", "Refused")) or not (banana or box):
            return call("observe")
        lost = box.startswith("not in view") if holding else banana.startswith("not in view")
        if lost:                                                   # search: look_around; if that did not help, sweep by hand
            if hist.count("look_around") < 2:
                return call("look_around")
            return call("turn", direction="left", degrees=90)
        if holding:
            return call("place") if "-> IN REACH" in box else call("walk_to", target="green_box")
        return call("pick") if "-> IN REACH" in banana else call("walk_to", target="banana")