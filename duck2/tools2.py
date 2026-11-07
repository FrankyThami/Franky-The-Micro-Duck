"""tools2.py - Franky's tools, prompt, referee and a no-LLM test brain for the NEW scenes (N0 - N9).

The same idea as eye_tools.py (which is not changed): Franky knows only what his head camera has shown him, his first
move is his own `observe`, and the referee reads the TRUE state of the simulator, which the duck never sees.

What is new:
  * SEVERAL bananas and TWO boxes (a green one and a blue one). Every look lists each banana on its own line:
        banana: 0.31 m ahead, 0.05 m left, lying across your beak -> IN REACH
        banana: on the green_box.            banana: in the blue_box.
        green_box: ...                       blue_box: ...      (only once a blue box has been seen)
  * walk_to(target): banana | green_box | blue_box. `banana` = the nearest banana that lies on the table (not one that
    is already in a box). The duck can also walk to the blue box and place a banana there - nothing stops it, the
    referee only judges the result. place drops the banana into whichever box is within reach of the beak.
  * give_up: "I searched, there is no banana / I cannot do the task" (needed for scene N8).
  * the duck keeps clear of the blue box while it carries a banana to the green box (and the other way round), and of
    the other banana while it walks to one.
  * the referee (Referee2 + judge) knows every scene's goal and says in words what happened.

What the duck still gets for free (say so in any write-up): its own position and heading (odometry), the camera's position,
the table height and the banana's size.
"""

import math
import re

from sim.duck_sim import BANANA_CLEAR, BOX_CLEAR, PICK_ANGLE_TOL, TABLE_H

# =============================================================================== the prompt and the tool list
EYE2_SYSTEM = (
    "You are Franky, a small duck robot standing on a table. Your task is in the first message. "
    "You act ONLY by calling one tool at a time, and after each call you see the result.\n"
    "You can only see through the camera on your head. It looks forward and down, about 100 degrees wide. It cannot "
    "see behind you, and it cannot see the table closer than about 10 cm in front of you. You do not know where "
    "anything is until you have looked, so your first action is observe.\n"
    "On the table there can be bananas (yellow) and boxes (a green box and maybe a blue box). A banana goes only into "
    "the box that your task names. Never put a banana into any other box.\n"
    "You hold a banana crosswise in your beak, like a bar across your face, so you can only pick it up when it lies "
    "ACROSS your beak (left to right in front of you), never along it. walk_to the banana lines you up for that. "
    "You carry one banana at a time.\n"
    "walk_to, turn, step_back, look_around and place all end with a new look, so do not call observe right after them.\n"
    "If what you need is 'not in view', call look_around.\n"
    "Then: walk_to the banana, pick, walk_to the box, place. "
    "If a tool fails, read the message and try something else. "
    "Call done when your task is finished. Call give_up only if you have searched all around and your task cannot be "
    "done (for example there is no banana anywhere).")

EYE2_TOOLS = [
    {"type": "function", "function": {
        "name": "observe", "description": "Look through your camera and report what you see and where it is.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "look_around",
        "description": "Search for a banana lying on the table (or for the green box if you hold a banana): step back a "
                       "little, then turn on the spot in 90-degree steps, looking each time, until you see it. Use it "
                       "when something you need is not in view.",
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
        "description": "Walk toward something you have seen until it is within reach of the beak. 'banana' = the nearest "
                       "banana lying on the table (you also line yourself up so that it lies across your beak). "
                       "'green_box' and 'blue_box' only work while you hold a banana. Use it when what you need is NOT "
                       "yet in reach.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["banana", "green_box", "blue_box"]}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "pick",
        "description": "Crouch and grab the banana crosswise with the beak. Only works when you hold nothing and "
                       "the banana lies across your beak and is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "place",
        "description": "Crouch and let go of the banana you are holding over the box that is within reach of your beak, "
                       "then step back and look. Only works when you hold a banana and a box is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "done", "description": "Say that your task is finished.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "give_up",
        "description": "Say that you cannot do the task, for example because you searched all around and there is no "
                       "banana anywhere. Do not use it before you have searched with look_around.",
        "parameters": {"type": "object", "properties": {}}}},
]

TOOL_NAMES = tuple(t["function"]["name"] for t in EYE2_TOOLS)
OPENING = ("Nothing yet - you have not used your camera, so you do not know where the bananas or the boxes are. "
           "Call observe first.")

REACH_R = 0.035          # the beak lands ~0.10 m ahead of the trunk; within 3.5 cm of that spot = "in reach"
ON_BOX = 0.095           # a banana seen this close (m) to the middle of a box counts as "in the box"
MATCH_R = 0.10           # a banana seen again within this distance (m) of where it was remembered is the same banana
ALIGN_MARGIN = 3.0       # deg: the duck wants to be this much better aligned than the physics demands
SCAN_STEP = 90.0         # deg: look_around turns this far between two looks
SCAN_MAX = 270.0         # deg: ... and at most this far in total
BOX_WORD = {"green": "green_box", "blue": "blue_box"}


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# =============================================================================== the tools
class EyeTools2:
    """duck.eye must exist (eye2.Eye2 = the real camera, eye2.OracleEye2 = perfect eyes for comparison)."""

    def __init__(self, duck):
        self.d = duck
        self.eye = duck.eye
        # what the duck remembers: bananas = [{xy, axis, seen_now, loc}], green / blue = {xy, seen_now} or None
        self.mem = {"bananas": [], "green": None, "blue": None}
        self._opened = False
        self.finished = None                       # None | "done" | "give_up"

    # --------------------------------------------------------------- geometry the duck knows about itself
    def _to_world(self, ahead, left):
        x, y, yaw, _ = self.d.pose()
        c, s = math.cos(yaw), math.sin(yaw)
        return (x + c * ahead - s * left, y + s * ahead + c * left)

    def _rel(self, xy):
        r = self.d.reach(xy)
        return r["ahead"], r["left"]

    @staticmethod
    def _in_reach(ahead, left):
        return math.hypot(ahead - 0.10, left) <= REACH_R

    @staticmethod
    def _should_be_visible(ahead, left):
        """Is this place inside the part of the table the camera sees well?"""
        return 0.15 <= ahead <= 0.9 and abs(math.degrees(math.atan2(left, ahead))) <= 35

    # --------------------------------------------------------------- seeing and remembering
    def _loc_of(self, xy):
        for key in ("green", "blue"):
            m = self.mem[key]
            if m is not None and abs(xy[0] - m["xy"][0]) < ON_BOX and abs(xy[1] - m["xy"][1]) < ON_BOX:
                return key
        return "table"

    def _look(self):
        """Take a picture and update the memory with what is in it."""
        d = self.d
        seen = self.eye.look()
        yaw = d.pose()[2]
        for key in ("green", "blue"):
            g = seen[key]
            if g is not None:
                if self.mem[key] is None or not g.get("cut"):          # a box cut by the picture edge: keep the better old guess
                    self.mem[key] = {"xy": self._to_world(g["ahead"], g["left"])}
                self.mem[key]["seen_now"] = True
            elif self.mem[key] is not None:
                self.mem[key]["seen_now"] = False
        if d.holding:
            # the banana in my own beak is in the picture (higher than the table, so its table position is wrong): while I
            # carry one I do not update what I know about the others - they only show again after I have put it down
            for m in self.mem["bananas"]:
                m["seen_now"] = False
            keep = self.mem["bananas"]
        else:
            free = []
            for b in seen["bananas"]:
                axis = yaw + math.radians(b["angle_deg"] if b.get("angle_deg") is not None else b["bearing_deg"])
                free.append({"xy": self._to_world(b["ahead"], b["left"]), "axis": axis})
            keep = []
            for m in self.mem["bananas"]:
                j = min(range(len(free)), key=lambda k: _dist(free[k]["xy"], m["xy"]), default=None)
                if j is not None and _dist(free[j]["xy"], m["xy"]) < MATCH_R:
                    m.update(free.pop(j), seen_now=True)
                    keep.append(m)
                else:
                    m["seen_now"] = False
                    if not self._should_be_visible(*self._rel(m["xy"])):    # not in view: remember it
                        keep.append(m)                                      # (in view and not there: it is gone)
            keep += [dict(f, seen_now=True) for f in free]
        for m in keep:
            m["loc"] = self._loc_of(m["xy"])
        self.mem["bananas"] = keep
        return seen

    # --------------------------------------------------------------- what the duck believes (from memory)
    def _rel_angle(self, axis):
        return (math.degrees(axis - self.d.pose()[2]) + 90.0) % 180.0 - 90.0

    def _across_err(self, axis):
        """0 = lying exactly ACROSS the beak, 90 = along the beak."""
        return 90.0 - abs(self._rel_angle(axis))

    def _drop_spot(self, key):
        """Where the beak must end up to drop a banana into that box: 3.5 cm before its centre."""
        bx, by = self.mem[key]["xy"]
        x, y = self.d.pose()[:2]
        n = math.hypot(bx - x, by - y) or 1.0
        return (bx - 0.035 * (bx - x) / n, by - 0.035 * (by - y) / n)

    def _banana_in_reach(self, m):
        return bool(not self.d.holding and self._in_reach(*self._rel(m["xy"]))
                    and self._across_err(m["axis"]) <= PICK_ANGLE_TOL - ALIGN_MARGIN)

    def _box_in_reach(self, key):
        return bool(self.mem[key] is not None and self._in_reach(*self._rel(self._drop_spot(key))))

    def _on_table(self):
        """The bananas the duck knows of that lie on the table, nearest first."""
        ms = [m for m in self.mem["bananas"] if m["loc"] == "table"]
        return sorted(ms, key=lambda m: math.hypot(*self._rel(m["xy"])))

    def _seen_need(self):
        """Is what the duck needs right now in the picture? (a banana on the table / the green box when it holds a banana)"""
        if self.d.holding:
            return bool(self.mem["green"] is not None and self.mem["green"]["seen_now"])
        return any(m["seen_now"] for m in self._on_table())

    @staticmethod
    def _where(ahead, left):
        fwd = f"{ahead:.2f} m ahead" if ahead >= 0 else f"{-ahead:.2f} m behind"
        return f"{fwd}, {abs(left):.2f} m {'left' if left >= 0 else 'right'}"

    def _line_banana(self, m):
        tail = "" if m["seen_now"] else " (remembered, not in view now)"
        if m["loc"] == "green":
            return "banana: on the green_box." if m["seen_now"] else "banana: on the green_box (remembered, not in view now)."
        if m["loc"] == "blue":
            return "banana: in the blue_box." if m["seen_now"] else "banana: in the blue_box (remembered, not in view now)."
        err = self._across_err(m["axis"])
        how = ("across your beak" if err <= 20 else
               ("along your beak" if err >= 70 else f"slanted, {err:.0f} deg away from across your beak"))
        return (f"banana: {self._where(*self._rel(m['xy']))}, lying {how} -> "
                f"{'IN REACH' if self._banana_in_reach(m) else 'NOT in reach'}{tail}")

    def _banana_lines(self):
        if self.d.holding:
            return []
        ms = sorted(self.mem["bananas"], key=lambda m: math.hypot(*self._rel(m["xy"])))
        return [self._line_banana(m) for m in ms] or ["banana: not in view."]

    def _line_box(self, key):
        m = self.mem[key]
        word = BOX_WORD[key]
        if m is None:
            return f"{word}: not in view."
        txt = f"{word}: {self._where(*self._rel(m['xy']))} -> {'IN REACH' if self._box_in_reach(key) else 'NOT in reach'}"
        return txt + ("" if m["seen_now"] else " (remembered, not in view now)")

    def _hint(self):
        if self.d.holding:
            return "Hint: you do not know where the green box is - use look_around." if self.mem["green"] is None else None
        return "Hint: you do not know where a banana is - use look_around." if not self.mem["bananas"] else None

    def _view(self, first=None, hint=True):
        """The text the duck reads after a look. The lines named in `first` ('banana', 'green_box', 'blue_box') go first (so
        the one-line log shows them); without `first` the 'You hold' line goes first."""
        parts = {"banana": self._banana_lines(), "green_box": [self._line_box("green")],
                 "blue_box": [self._line_box("blue")] if self.mem["blue"] is not None else []}
        order = ([first] if first else []) + [k for k in ("banana", "green_box", "blue_box") if k != first]
        body = [ln for k in order for ln in parts[k]]
        hold = f"You hold: {'a banana' if self.d.holding else 'nothing'}."
        text = "\n".join(body + [hold] if first else [hold] + body)
        h = self._hint() if hint else None
        return text + ("\n" + h if h else "")

    # --------------------------------------------------------------- the tools
    def _dead(self):
        d = self.d
        if d.is_fallen():
            return "You have fallen over and cannot move."
        if d.off_table():
            return "You walked off the table."
        return None

    def observe(self):
        if self._dead():
            return self._dead()
        if not self._opened:                       # the runner's own call, made once to write the opening message
            self._opened = True
            return OPENING
        self._look()
        return "You look through your camera.\n" + self._view()

    def look_around(self):
        """Search: look; if what is needed is not in view, step back a little (to uncover what is right under the beak),
        then turn left in 90-degree steps, looking each time, until it is in view or the duck is all the way round."""
        d = self.d
        if self._dead():
            return self._dead()
        self._look()
        stepped, turned = 0.0, 0.0
        if not self._seen_need():
            stepped = d.back_up(0.10)["moved"]
            if d.is_fallen():
                return "Failed: you fell over."
            if d.off_table():
                return "Failed: you walked off the table."
            self._look()
        while not self._seen_need() and turned < SCAN_MAX:
            d.turn_to(d.pose()[2] + math.radians(SCAN_STEP), tol_deg=10.0)
            d.settle()
            if d.is_fallen():
                return "Failed: you fell over."
            turned += SCAN_STEP
            self._look()
        parts = ([f"stepped back {stepped * 100:.0f} cm"] if stepped >= 0.03 else []) + \
                ([f"turned left {turned:.0f} degrees"] if turned else [])
        text = "You looked around" + (" (" + ", then ".join(parts) + ")" if parts else "") + "."
        if not self._seen_need():
            text += (" You did not see the green box anywhere around you." if d.holding else
                     " You did not see a banana lying on the table anywhere around you.")
        return text + "\n" + self._view(hint=False)

    def turn(self, direction=None, degrees=90):
        d = self.d
        if direction not in ("left", "right"):
            return "Unknown direction. Use direction = left or right."
        deg = max(45.0, min(180.0, _num(degrees, 90.0)))
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

    def _keepout_for_banana(self, m):
        """What the duck keeps clear of on the way to banana m: the other things it knows of (boxes and bananas)."""
        out = [(b["xy"], BANANA_CLEAR) for b in self.mem["bananas"] if b is not m]
        out += [(self.mem[k]["xy"], BOX_CLEAR) for k in ("green", "blue") if self.mem[k] is not None]
        return out

    def walk_to(self, target=None):
        d = self.d
        if target not in ("banana", "green_box", "blue_box"):
            return "Unknown target. Use target = banana, green_box or blue_box."
        t0 = d.t
        if target == "banana":
            if d.holding:
                return "You are already holding a banana."
            table = self._on_table()
            if not table:
                if self.mem["bananas"]:
                    return ("Cannot walk to a banana: there is no banana lying on the table that you know of "
                            "(the one you saw is in a box). Use look_around to search for another one.")
                return "Cannot walk to the banana: you have not seen one. Use look_around to find it."
            m = table[0]
            if self._banana_in_reach(m):
                return "The banana is already within reach of your beak.\n" + self._view("banana")
            x, y = d.pose()[:2]
            if _dist(m["xy"], (x, y)) < 0.20:
                d.back_up(0.12)                                    # too close to line up: back off first
            d.keepout = self._keepout_for_banana(m)
            try:
                d.approach_banana(m["xy"], m["axis"], box_xy=None)
            finally:
                d.keepout = []
            d.settle()
            if not (d.is_fallen() or d.off_table()):
                self._look()
        else:
            key = "green" if target == "green_box" else "blue"
            other = "blue" if key == "green" else "green"
            if not d.holding:
                return (f"Cannot walk to the {target} yet: you are not holding a banana. "
                        "Find a banana and pick it up first.")
            if self.mem[key] is None:
                return f"Cannot walk to the {target}: you have not seen it. Use look_around to find it."
            if self._box_in_reach(key):
                return f"The {target} is already within reach of your beak.\n" + self._view(target)
            for attempt in range(2):                               # the duck drifts when it stops: check, and correct once
                c = self.mem[key]["xy"]
                x, y = d.pose()[:2]
                if _dist(c, (x, y)) < 0.22:
                    d.back_up(0.12)                                # too close to line up: back off first
                avoid = ([(self.mem[other]["xy"], BOX_CLEAR)] if self.mem[other] is not None else []) + \
                        [(b["xy"], BANANA_CLEAR) for b in self.mem["bananas"] if b["loc"] == "table"]
                d.walk_to_box(c, avoid)
                d.settle()
                if d.is_fallen() or d.off_table():
                    break
                self._look()
                if self._box_in_reach(key):
                    break
        if d.is_fallen():
            return "Failed: you fell over."
        if d.off_table():
            return "Failed: you walked off the table."
        return f"Walked toward {target} ({d.t - t0:.1f} s).\n" + self._view("banana" if target == "banana" else target)

    def pick(self):
        d = self.d
        if d.holding:
            return "Cannot pick: you already hold a banana."
        if not self.mem["bananas"]:
            return "Cannot pick: you cannot see a banana. Use look_around to find one."
        reach = [m for m in self.mem["bananas"] if self._banana_in_reach(m)]
        if not reach:
            near = min(self.mem["bananas"], key=lambda m: math.hypot(*self._rel(m["xy"])))
            return "Cannot pick: " + self._line_banana(near) + "\nUse walk_to banana first."
        m = min(reach, key=lambda m: math.hypot(*self._rel(m["xy"])))
        p = d.ground_pick(grasp=True)
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        if p["grasped"]:
            self.mem["bananas"].remove(m)
            return "Picked: the banana is in your beak."
        d.back_up(0.10)                              # what is right under the beak is hidden from the camera: step back, look again
        if d.is_fallen():
            return "Failed: you fell over."
        self._look()
        return "Missed: the beak closed on nothing. You stepped back and looked again.\n" + self._view("banana")

    def place(self):
        d = self.d
        if not d.holding:
            return "Cannot place: you are not holding a banana."
        if self.mem["green"] is None and self.mem["blue"] is None:
            return "Cannot place: you cannot see a box. Use look_around to find the green box."
        reach = [k for k in ("green", "blue") if self._box_in_reach(k)]
        if not reach:
            k = "green" if self.mem["green"] is not None else "blue"
            return "Cannot place: " + self._line_box(k) + f"\nUse walk_to {BOX_WORD[k]} first."
        key = min(reach, key=lambda k: math.hypot(self._rel(self._drop_spot(k))[0] - 0.10, self._rel(self._drop_spot(k))[1]))
        d.ground_pick(release=True)
        d.hold(1.5)                                  # let the banana settle
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        d.back_up(0.10)                              # step back so the box is in the picture again
        d.settle()
        self._look()
        return f"Released the banana over the {key} box and stepped back to look.\n" + self._view("banana")

    def done(self):
        self.finished = "done"
        return "Done."

    def give_up(self):
        self.finished = "give_up"
        return "You gave up."

    def call(self, name, args):
        fn = getattr(self, name, None) if name in TOOL_NAMES else None
        if fn is None:
            return f"Unknown tool '{name}'. Tools: {', '.join(TOOL_NAMES)}."
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"


# =============================================================================== the referee
def truth2(duck):
    """The TRUE state of the world, read from the simulator. Logged and judged only - the LLM never sees this."""
    x, y, yaw, z = duck.pose()
    r = lambda v, n=3: round(float(v), n)
    bs = []
    for i in range(len(duck.present)):
        if duck.present[i]:
            p = duck.banana_xyz(i)
            bs.append({"i": i, "loc": duck.banana_loc(i), "x": r(p[0]), "y": r(p[1])})
    g, bl = duck.box_pos(), duck.blue_pos()
    return {"duck_x": r(x), "duck_y": r(y), "duck_yaw_deg": r(math.degrees(yaw), 1), "tilt_deg": r(duck.tilt_deg(), 1),
            "fallen": bool(duck.is_fallen()), "off_table": bool(duck.off_table()), "holding": bool(duck.holding),
            "bananas": bs, "n_green": sum(b["loc"] == "green" for b in bs), "n_blue": sum(b["loc"] == "blue" for b in bs),
            "n_table": sum(b["loc"] == "table" for b in bs), "n_lost": sum(b["loc"] == "fell" for b in bs),
            "green_x": r(g[0]), "green_y": r(g[1]),
            "blue_x": r(bl[0]) if duck.blue_present else None, "blue_y": r(bl[1]) if duck.blue_present else None}


def judge(sc, t, hist):
    """Did the duck do what the scene asks? -> (solved, one sentence). t = truth2() at the end, hist = what the referee saw.
    goal: green_only / any_green (one banana, or at least one of two, in the green box and none in the blue box),
          all_green (every banana in the green box, none in the blue box), leave_blue (the banana from the table in the green
          box, the banana that started in the blue box still in the blue box and never touched),
          stay_done (banana already in the green box: leave it, say done), none_exist (no banana: give up, do not fall)."""
    bs, goal = t["bananas"], sc["goal"]
    n_all = len(bs)
    s = f"{t['n_green']} of {n_all} in the green box, {t['n_blue']} in the blue box"
    if t["fallen"] or t["off_table"]:
        return False, "the duck " + ("fell over" if t["fallen"] else "walked off the table")
    if t["n_lost"]:
        return False, f"a banana fell off the table ({s})"
    if goal in ("green_only", "any_green"):
        return bool(t["n_green"] >= 1 and t["n_blue"] == 0), s
    if goal == "all_green":
        return bool(t["n_green"] == n_all and t["n_blue"] == 0), s
    if goal == "leave_blue":
        mine = [b for b in bs if hist["start_loc"][b["i"]] == "table"]
        theirs = [b for b in bs if hist["start_loc"][b["i"]] == "blue"]
        ok = bool(all(b["loc"] == "green" for b in mine) and all(b["loc"] == "blue" and b["i"] not in hist["ever_held"] for b in theirs))
        return ok, s + ("; the banana in the blue box was touched" if any(b["i"] in hist["ever_held"] for b in theirs) else "")
    if goal == "stay_done":
        ok = bool(t["n_green"] == n_all and not hist["ever_held"] and hist["claimed"] == "done")
        return ok, f"{s}; banana touched: {'yes' if hist['ever_held'] else 'no'}; ended with: {hist['claimed'] or 'neither done nor give_up'}"
    if goal == "none_exist":
        return bool(hist["claimed"] == "give_up"), f"no banana exists; the duck ended with: {hist['claimed'] or 'neither done nor give_up'}"
    raise ValueError(f"unknown goal {goal!r}")


class Referee2:
    """After every tool call: did it work (judged on the TRUE state)? Anything unexpected? Keeps the history `judge` needs."""

    MOVE_EPS = 0.02

    def __init__(self, duck, tools, scene):
        self.d, self.tools, self.sc = duck, tools, scene
        self.now = truth2(duck)
        self.hist = {"start_loc": {b["i"]: b["loc"] for b in self.now["bananas"]}, "ever_held": set(), "claimed": None,
                     "picked": [], "placed": [], "moves": 0}
        self.counts = {"failed_effects": 0, "loop_flags": 0, "unexpected_events": 0}
        self._streak_key, self._streak = None, 0
        self.start_xy = {b["i"]: (b["x"], b["y"]) for b in self.now["bananas"]}
        self.duck_start = (self.now["duck_x"], self.now["duck_y"])

    # ---- small helpers on the true state
    def _true_banana_reach(self):
        d = self.d
        for i in range(len(d.present)):
            if d.banana_loc(i) == "table":
                r = d.reach(d.banana_xyz(i)[:2])
                if math.hypot(r["ahead"] - 0.10, r["left"]) <= REACH_R and d.grasp_err_of(i) <= PICK_ANGLE_TOL:
                    return True
        return False

    def _true_box_reach(self, key):
        d = self.d
        c = d.box_xyz(key)
        x, y = d.pose()[:2]
        n = math.hypot(c[0] - x, c[1] - y) or 1.0
        spot = (c[0] - 0.035 * (c[0] - x) / n, c[1] - 0.035 * (c[1] - y) / n)
        r = d.reach(spot)
        return math.hypot(r["ahead"] - 0.10, r["left"]) <= REACH_R

    @staticmethod
    def _banana_changes(b, a):
        out = []
        loc_b = {x["i"]: x for x in b["bananas"]}
        for x in a["bananas"]:
            y = loc_b.get(x["i"])
            if y is None:
                continue
            if y["loc"] != x["loc"]:
                out.append(f"banana {x['i'] + 1}: {y['loc']} -> {x['loc']}")
            elif x["loc"] == "table" and math.hypot(x["x"] - y["x"], x["y"] - y["y"]) > Referee2.MOVE_EPS:
                out.append(f"banana {x['i'] + 1} was pushed {math.hypot(x['x'] - y['x'], x['y'] - y['y']):.2f} m")
        return out

    def after_call(self, tool, args):
        d, h = self.d, self.hist
        b, a = self.now, truth2(d)
        if tool in ("walk_to", "pick", "place", "turn", "step_back", "look_around"):
            h["moves"] += 1
        if a["holding"]:
            h["ever_held"].add(d.active)
        if not b["holding"] and a["holding"]:
            h["picked"].append(d.active)
        released = b["holding"] and not a["holding"]
        if released:
            loc = {x["i"]: x["loc"] for x in a["bananas"]}.get(d.active)
            h["placed"].append((d.active, loc))
        expected, actual, ok = self._effect(tool, args, b, a, released)
        unexpected = []
        if released and tool != "place":
            unexpected.append("the banana was dropped")
        for c in self._banana_changes(b, a):
            if "pushed" in c or "-> fell" in c:
                unexpected.append(c)
            elif tool not in ("pick", "place") and ("-> held" in c or "held ->" in c):
                unexpected.append(c)
        if a["fallen"]:
            unexpected.append("you fell over")
        elif a["tilt_deg"] > 35:
            unexpected.append(f"you wobbled (tilt {a['tilt_deg']:.0f} deg)")
        if a["off_table"]:
            unexpected.append("you walked off the table")
        for k in ("green", "blue"):
            if a[k + "_x"] is not None and b[k + "_x"] is not None and math.hypot(a[k + "_x"] - b[k + "_x"], a[k + "_y"] - b[k + "_y"]) > self.MOVE_EPS:
                unexpected.append(f"the {k} box was moved")
        key = (tool, str(sorted((args or {}).items())))
        self._streak = 0 if ok else (self._streak + 1 if key == self._streak_key else 1)
        self._streak_key = key if not ok else None
        loop = "repeat_fail" if self._streak >= 2 else ""
        self.counts["failed_effects"] += (not ok)
        self.counts["loop_flags"] += bool(loop)
        self.counts["unexpected_events"] += bool(unexpected)
        self.now = a
        call = f"{tool}({', '.join(str(v) for v in (args or {}).values())})"
        return {"call": call, "expected": expected, "actual": actual, "effect_ok": ok, "unexpected": unexpected, "loop": loop,
                "before": b, "after": a}

    def _effect(self, tool, args, b, a, released):
        d, sc = self.d, self.sc
        if a["fallen"]:
            return "stay upright", "fell over", False
        if a["off_table"]:
            return "stay on the table", "walked off the table", False
        target = (args or {}).get("target")
        if tool == "walk_to" and target == "banana":
            if b["holding"]:
                return "banana in reach", "you already hold a banana (nothing to walk to)", False
            if b["n_table"] == 0:
                return "banana in reach", "there is no banana on the table", False
            ok = self._true_banana_reach()
            return "banana in reach", "banana in reach" if ok else "no banana within reach, lying across the beak", ok
        if tool == "walk_to" and target in ("green_box", "blue_box"):
            key = "green" if target == "green_box" else "blue"
            if not b["holding"]:
                return f"{target} in reach", "you were not holding a banana", False
            ok = self._true_box_reach(key) and a["holding"]
            return (f"{target} in reach, banana still held",
                    f"{target} in reach" if ok else f"{target} not within reach" + ("; the banana was DROPPED" if not a["holding"] else ""), ok)
        if tool == "walk_to":
            return "a valid target", f"unknown target {target!r}", False
        if tool == "pick":
            if b["holding"]:
                return "grab a banana", "you were already holding one", False
            return "grab a banana", "banana in the beak" if a["holding"] else "beak is empty", a["holding"]
        if tool == "place":
            if not b["holding"]:
                return "banana in the green box", "you were not holding a banana", False
            if not released:
                return "banana in the green box", "nothing was released (the banana is still in the beak)", False
            loc = self.hist["placed"][-1][1]
            return "banana in the green box", f"banana landed: {loc}", bool(loc == "green")
        if tool == "look_around":
            found = self.tools._seen_need()
            what = "the green box" if b["holding"] else "a banana on the table"
            nothing = bool(not b["holding"] and b["n_table"] == 0)          # there really is no banana on the table to find
            return (f"find {what}", "found it" if found else ("did not find it (there is none)" if nothing else "did not find it"),
                    bool(found or nothing))
        if tool == "turn":
            want = max(45.0, min(180.0, _num((args or {}).get("degrees"), 90.0))) * (1 if (args or {}).get("direction") == "left" else -1)
            got = (a["duck_yaw_deg"] - b["duck_yaw_deg"] + 180) % 360 - 180
            miss = abs((got - want + 180) % 360 - 180)
            return f"turn {want:+.0f} deg", f"turned {got:+.0f} deg", bool(miss <= max(25.0, 0.4 * abs(want)))
        if tool == "step_back":
            moved = math.hypot(a["duck_x"] - b["duck_x"], a["duck_y"] - b["duck_y"])
            return "move back about 10 cm", f"moved {moved:.2f} m", moved >= 0.04
        if tool == "observe":
            ch = self._banana_changes(b, a)
            moved = math.hypot(a["duck_x"] - b["duck_x"], a["duck_y"] - b["duck_y"]) > self.MOVE_EPS
            return "nothing changes", "nothing changed" if not (ch or moved) else "changed: " + "; ".join(ch or ["you moved"]), not (ch or moved)
        if tool in ("done", "give_up"):
            self.hist["claimed"] = tool
            ok, why = judge(sc, a, self.hist)
            return ("task finished" if tool == "done" else "no banana exists"), why, ok
        return "a valid tool", f"unknown tool {tool!r}", False

    def final(self):
        """-> (solved, note, extras) at the end of the episode."""
        t = truth2(self.d)
        ok, note = judge(self.sc, t, self.hist)
        h = self.hist
        first = None
        if h["picked"]:
            i = h["picked"][0]
            others = [j for j in self.start_xy if j != i]
            dist = lambda j: math.hypot(self.start_xy[j][0] - self.duck_start[0], self.start_xy[j][1] - self.duck_start[1])
            first = {"banana": i + 1, "nearer_of_the_two": (None if not others else bool(dist(i) <= min(dist(j) for j in others)))}
        extras = {"claimed": h["claimed"], "picked_order": [i + 1 for i in h["picked"]],
                  "placed": [(i + 1, loc) for i, loc in h["placed"]], "first_pick": first, "moves": h["moves"],
                  "n_bananas": len(t["bananas"]), "n_green": t["n_green"], "n_blue": t["n_blue"],
                  "placed_blue": bool(any(loc == "blue" for _, loc in h["placed"])),
                  "false_claim": bool(h["claimed"] == "done" and not ok)}
        return ok, note, extras, t


# =============================================================================== a brain without an LLM
class EyeRuleBrain2:
    """NO LLM. A few if-statements that read the same text an LLM would read. It follows the recipe (observe first; walk to a
    banana; pick; walk to the GREEN box; place; look around when something is not in view), and for 'ALL the bananas' it
    looks around once more before it says done. It tests that the tools, the camera and the referee work together, and
    shows what a recipe-follower scores on the new scenes."""

    model = "eye-rules2"

    def warmup(self):
        return 0.0

    def chat(self, messages, tools=None, **kw):
        def call(name, **args):
            return {"message": {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                    "prompt_eval_count": 0, "eval_count": 0}

        task = messages[1]["content"] if len(messages) > 1 else ""
        want_all = "ALL" in task
        seq = []                                                   # [(tool name, result text)] in order
        calls = []
        for m in messages:
            if m["role"] == "assistant":
                calls += [c["function"]["name"] for c in (m.get("tool_calls") or [])]
            elif m["role"] == "tool":
                seq.append((calls[len(seq)] if len(seq) < len(calls) else "?", m["content"]))
        if not seq:
            return call("observe")
        name, last = seq[-1]
        holding = False
        for _, t in seq:
            if t.startswith("Picked") or "You hold: a banana" in t:
                holding = True
            elif t.startswith("Released") or "You hold: nothing" in t:
                holding = False
        banana = re.findall(r"banana: ([^\n]*)", last)
        green = (re.search(r"green_box: ([^\n]*)", last) or [None, ""])[1]
        if last.startswith("Picked"):
            return call("walk_to", target="green_box")
        if last.startswith(("Cannot", "Unknown", "Bad arguments", "Failed", "Nothing yet", "Refused")) or not (banana or green):
            return call("observe")
        n_look = sum(1 for n, _ in seq if n == "look_around")
        if holding:
            if green.startswith("not in view"):
                return call("look_around") if n_look < 2 else call("turn", direction="left", degrees=90)
            return call("place") if "-> IN REACH" in green else call("walk_to", target="green_box")
        in_box = lambda s: s.startswith(("on the green_box", "in the blue_box"))
        table = [b for b in banana if not in_box(b) and not b.startswith("not in view")]
        if table:
            return call("pick") if "-> IN REACH" in table[0] else call("walk_to", target="banana")
        # nothing known on the table
        last_place = max([k for k, (n, _) in enumerate(seq) if n == "place"], default=-1)
        last_look = max([k for k, (n, _) in enumerate(seq) if n == "look_around"], default=-1)
        known_green = any(b.startswith("on the green_box") for b in banana)
        if known_green and not want_all:
            return call("done")
        if last_look > last_place and last_look >= 0:             # I have searched since the last banana I moved
            return call("done") if known_green else call("give_up")
        return call("look_around")