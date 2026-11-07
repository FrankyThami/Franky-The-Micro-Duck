"""franky_tools.py - Franky's tools for AUTONOMOUS operation (the brain is the LLM, nothing else decides).

What this file is NOT any more (all of it was in the old eye_tools.py):
  * no recipe in the prompt ("then walk_to the banana, pick, walk_to the green_box, place ...")
  * no ready-made judgements ("IN REACH", "NOT in reach", "banana: on the green_box")
  * no colour detection that tells the duck what it sees, no memory of objects kept by code
  * no walk_to(banana) that plans the path and picks the side to come from
  * no rule brain (EyeRuleBrain) that could solve the scenes with if-statements

What the LLM has:
  GOAL            the command a person gives (the first message of every episode)
  CAMERA          facts about what the camera can and cannot see (SYSTEM_PROMPT)
  BODY            facts about the body: how the beak holds the banana, where it touches down, what can trip it (SYSTEM_PROMPT)
  TOOLS           look, find, measure, walk, turn, close_beak, open_beak, done - their descriptions are in TOOLS below (turn: version 12, switch --turn-tool).
Every tool gives back RAW data: the picture itself, distances and bearings in metres and degrees, what the body really did
(odometry), and what the beak closed on. Reading it, deciding what it means and what to do next is the LLM's job.

The picture goes to the vision model as a real image. Two tools turn the picture into real distances, and neither knows what is in it:
  * `measure`  the model points at a spot (it decides which pixel is the banana, which is a blue box ...); the tool answers with
               pure geometry - where the camera ray through that pixel touches the table.
  * `find`     the model names a thing; Gemma is asked, in its NATIVE detection mode (JSON boxes, 0-1000), to outline it in the
               picture; the tool then does the same geometry on the box. Added after the first real run showed that Gemma e2b
               points badly through a function call but is trained to give boxes. Switch it off with --find off.

The beak tools are called close_beak / open_beak (not pick / place): the first real run showed that a 2B model reads "pick" as a finished skill
("pick up the banana") and called it again and again from 40 cm away without ever walking. The names now say what the body does.

What the duck still gets for free (say so in any write-up): the camera's position and direction and its own odometry (a real
robot has them from its joint angles and wheel / leg odometry), the table height, and a table-edge / box safety layer.
A short-range BEAK SENSE (new in version 5, switch it off with --beak-sense off): when the beak closes and does not take the banana, the
result says where the banana's middle was relative to the beak tip and how its long side pointed - if the banana was within 0.10 m of the
tip, which is the strip of table the camera cannot see - otherwise only that it was farther away. They are measurements from the
simulator, given as raw numbers (no advice, no "too far / wrong angle"), like the measure tool's. It is the only place where the true
state reaches the duck during an attempt, and it costs a crouch each time (the guards count a failed close like any failed action).
Reason: in the real run (3 attempts) the beak closed without taking the banana 11 times, always with the banana lying along the duck's
heading (4 to 32 degrees off, the grasp needs about 90 +- 20), at some of them also about 4 cm to the side; "not under your beak tip,
or not across your beak" never told the duck which of the two it was.
VERSION 6 (after the fifth real run, the first one with --think off): the beak sense is reported after a SUCCESSFUL close too, in the same words
(a success used to say nothing, so the reflection could not compare the one grasp with the 11 misses and wrote a wrong reason for it); a failed
close says plainly that the banana is still on the table; `find` cuts a box that the vision model drew into the grey bars at the picture's edge
(the banana at the bottom edge was reported as "in the grey padding" and cost a step each time); the descriptions of `find` and `measure` say that
the angle of a two-spot line belongs to the duck's own two spots (the duck had measured the left and right edge of a box, got +89 degrees, and
believed the banana was across its beak when it lay along it).
VERSION 8 (after the seventh real run): every `walk` result says how much the body is turned in total since the start of the episode ("Total turned since
the start: 22 degrees to the left"; left minus right, from the body's own heading, i.e. odometry). Each walk turned the duck 2-11 degrees to the left of what it asked,
so after 4-6 walks its heading had drifted 22-30 degrees and the banana, which lay exactly across its start heading, looked 60-68 degrees instead of 70-90:
five closes within 5 cm of the banana missed for that reason. The line states a number and gives no advice.
VERSION 10 (after the ninth real run): the PRACTICE scenes (10, 11, 12) can use a wider grasp window than the benchmark (scene key `grasp` = (simulator radius in m, degrees,
stated reach in m); the benchmark is 5 cm and 20 degrees). In seven replays of sensible moves in scene 12 not one first close took the banana (a miss by 1 degree, or 4-10 cm
sideways), so the duck never reached carry and place. The simulator measures the radius in 3-D from the mouth point, which is a little above the table and, with the head
down, about 3 cm BEHIND the nominal beak tip, so what is true along the table is smaller than the radius: at the benchmark's 5 cm the banana's middle is taken from 4 cm to
the left, 3 to the right, 2 ahead and 9 behind the nominal tip. The practice radius of 10 cm reaches 10 left, 9 right, 7 ahead, 10 behind, so the duck is told "about 7 cm"
(the smallest reach), never a number that is not true. texts_for() puts the scene's stated window into the two places where the duck is told the rule; the benchmark scenes
and their texts are unchanged (they still say "5 cm", which is a little more than is true ahead and to the sides: see README).
VERSION 11 (after the tenth real run: the duck took the banana for the first time, in 3 of 6 attempts, and then never found the box - it lies about 85 degrees to the right,
outside the 100-degree picture, and the duck turned right once, by 15): (1) the reflection is given the same camera and body facts as the acting duck (body_facts()), because
the reflecting duck had only the log and wrote "the vision model fails to find the box" for what was a box outside the picture; (2) practice scene 13 "carry-start": the duck
starts already holding the banana, the box out of view on its right (scene key `hold`, see scene_benchmark.reset_scene); (3) the "Unknown tool" answer ends with "(To turn,
give walk a turn number.)" - twice the duck tried to turn right with a tool called `turn`, which does not exist, and gave the turn up.
VERSION 12 (after the eleventh real run: the first success, scene 13 at attempt 4): the `turn` tool: turn on the spot to the "left" or "right" by a positive number of degrees,
and a new picture = walk(forward=0, turn=+-degrees) with the answer in words (same motion, safety layer, referee and guard as walk; walk can still turn). Why: in that run the duck
wrote "turn right" at least four times and passed a positive number, which turns LEFT (positive = left in walk), so it looked away from a box that stood on its right; the right
turns that worked were negative numbers. A direction word and a positive number take the sign trap away. `turn` is in every scene when it is on (--turn-tool on, the default),
the benchmark included; --turn-tool off gives the old seven tools (and the "Unknown tool" answer then points to walk's turn number).
The referee (agent_harness/referee.py) reads the TRUE simulator state; the duck never sees it.
"""

import json
import math
import re

import copy

from sim import duck_sim, motion
from sim.duck_sim import PICK_ANGLE_TOL

DEFAULT_COMMAND = "Pick up the banana and put it into the green box."

# =============================================================================== the prompt: camera + body facts, nothing else
SYSTEM_PROMPT = (
    "You are Franky, a small duck robot standing on a table top. A person gives you a command. You carry it out on your own "
    "by calling tools, exactly ONE tool per turn. After every tool call you get its result, and usually a new picture from "
    "your camera. Nothing else tells you about the world.\n"
    "\n"
    "YOUR CAMERA\n"
    "- One camera on your head, 0.24 m above the table. It looks forward and tilted down, about 100 degrees wide.\n"
    "- You cannot see behind you, or anything outside the picture. The lowest edge of the picture shows the table about "
    "0.09 m in front of the centre of your body, so the strip of table right below your beak is hidden, and so is "
    "anything you hold in your beak.\n"
    "- A picture is flat. It does not tell you distances. The measure tool turns a spot in the picture into a real "
    "distance and direction.\n"
    "- Things far away are small in the picture, so a spot you point at there is only roughly where you meant. The closer "
    "you are, the larger and clearer things are.\n"
    "- A picture, and the measurements made on it, describe the moment it was taken. When you move, they are out of date.\n"
    "\n"
    "YOUR BODY\n"
    "- You walk on two legs. You can walk straight forward or backward and turn left or right. You cannot step sideways. "
    "Your movements are not exact: you end up a few centimetres and a few degrees away from what you asked for. The tool "
    "tells you what your body really did.\n"
    "- When you crouch to close or open your beak, your beak tip touches the table 0.10 m in front of the centre of your "
    "body. Closing and opening the beak only work on what is at that spot: they do not move you.\n"
    "- You hold the banana crosswise in your beak, like a bar across your face with its middle on your beak tip. So your "
    "beak can only take the banana when it lies ACROSS your beak (its long side at right angles to the direction you face, "
    f"within about {PICK_ANGLE_TOL:.0f} degrees) and its middle is within about 5 cm of your beak tip.\n"
    "- Your feet reach about 6 cm in front of the centre of your body. The rim of a box can trip you, and walking into the "
    "banana kicks it away. Stay on the table: past its edge is a long fall. A safety system refuses or stops moves that "
    "would take you too close to an edge or onto a box.\n"
    "\n"
    "Think about what you see, and what each move will do, before you act. Your thinking is not remembered from one turn to "
    "the next, and old pictures are dropped to save memory: the note you write in every tool call is what you will have.")

NOTE = {"type": "string", "description": "A short note to yourself, kept in your history: what you see and know so far (with the numbers), and what you will do next."}

TOOLS = [
    {"type": "function", "function": {
        "name": "look",
        "description": "Take a picture with your camera and look at it. Returns the picture.",
        "parameters": {"type": "object", "properties": {"note": NOTE}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "find",
        "description": "Ask a vision model to find something in the latest picture by name. Say what to look for in a few words "
                       "(its colour and kind, for example 'red cup'). The vision model draws a box around each match. For every box "
                       "you get: where it is in the picture (x and y on the same 0 to 1000 scale as measure), where the middle of "
                       "the box touches the table (metres ahead and to the left of the centre of your body, its distance and "
                       "bearing), and the size of its footprint on the table (how wide across, how deep along your line of sight, "
                       "in metres). You also get the picture back with the boxes drawn and numbered. The boxes are drawn by a "
                       "vision model and can be rough or wrong: check them on the picture. A box is drawn upright, so for a long "
                       "object it gives a footprint but not reliably which way the object points: for that, use measure with one "
                       "spot on each end of the object. Only things inside the picture can be found.",
        "parameters": {"type": "object", "properties": {
            "what": {"type": "string", "description": "what to look for, in a few words, e.g. its colour and kind"},
            "note": NOTE}, "required": ["what", "note"]}}},
    {"type": "function", "function": {
        "name": "measure",
        "description": "Measure real distances on the latest picture. Point at one spot (x, y), or at two spots (x, y and "
                       "x2, y2 - for example the two ends of an object). Positions in the picture are on a 0 to 1000 scale: "
                       "x = 0 is the left edge and x = 1000 the right edge, y = 0 is the top edge and y = 1000 the bottom edge. "
                       "Point at the place where the object touches the table. For every spot you get how far ahead and how "
                       "far to the left it is on the table (metres, from the centre of your body), its distance and its "
                       "bearing (degrees, positive = left). For two spots you also get the middle between them, the length "
                       "of the line between them and its angle relative to the direction you face (0 = pointing the way you "
                       "face, +90 or -90 = lying across you; positive = the line is rotated toward your left). That angle belongs "
                       "to the line between YOUR two spots: it describes an object only if the spots are on the object's two "
                       "opposite ends. You also get "
                       "the picture back with your spots marked by red crosses, so you can check you pointed at the right place. "
                       "Only a spot on the table surface can be measured.",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "number", "description": "first spot, across the picture, 0 (left) to 1000 (right)"},
            "y": {"type": "number", "description": "first spot, down the picture, 0 (top) to 1000 (bottom)"},
            "x2": {"type": "number", "description": "optional second spot, across the picture, 0 to 1000"},
            "y2": {"type": "number", "description": "optional second spot, down the picture, 0 to 1000"},
            "note": NOTE}, "required": ["x", "y", "note"]}}},
    {"type": "function", "function": {
        "name": "walk",
        "description": "Move your body. First you turn on the spot by `turn` degrees (positive = left, negative = right), then you "
                       "walk straight `forward` metres (negative = backwards). Either can be 0. Returns what your body really "
                       "did, measured by your own motion sensors (it differs a little from what you asked), how much your body is turned in "
                       "total since the start (all your turns added up, left minus right), and a new picture. "
                       "Limits: forward from -0.25 to 0.60 m; turn from -180 to 180 degrees. Turning on the spot by less than "
                       "about 30 degrees does not work well: a small turn is made gently while walking, so it needs forward "
                       "of at least 0.10 m. A very short walk (under about 0.05 m) is imprecise too: it can swing your heading by "
                       "10 degrees or more.",
        "parameters": {"type": "object", "properties": {
            "forward": {"type": "number", "description": "metres to walk straight after the turn; negative = backwards; default 0"},
            "turn": {"type": "number", "description": "degrees to turn first; positive = left, negative = right; default 0"},
            "note": NOTE}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "turn",
        "description": "Turn your body on the spot, to the `direction` you choose (\"left\" or \"right\"), by `degrees` (0 to 180, always a "
                       "positive number), and take a new picture. It does not move you forward. Returns what your body really did, measured by "
                       "your own motion sensors (it differs a little from what you asked), how much your body is turned in total since the start "
                       "(all your turns added up, left minus right), and a new picture. Turning on the spot by less than about 30 degrees does "
                       "not work well: a small turn is made gently while walking, so for a small turn use walk with a forward distance of at "
                       "least 0.10 m.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["left", "right"], "description": "the way to turn: left or right"},
            "degrees": {"type": "number", "description": "how far to turn, 0 to 180; never negative"},
            "note": NOTE}, "required": ["direction", "degrees", "note"]}}},
    {"type": "function", "function": {
        "name": "close_beak",
        "description": "Crouch, close your beak on whatever is under your beak tip (the spot on the table 0.10 m in front of the "
                       "centre of your body), and stand up. It does not move you and does not aim at anything: it only closes the "
                       "beak where the beak tip is. The beak takes the banana only if the banana's middle is within about 5 cm of "
                       f"your beak tip and it lies across your beak (within about {PICK_ANGLE_TOL:.0f} degrees). Returns whether the "
                       "banana was taken. Your beak also senses the banana close to your beak tip, a strip the camera cannot see: "
                       "when the banana's middle is within 0.10 m of your beak tip (whether it was taken or not) you get where it was "
                       "relative to your beak tip and the angle of its long side, otherwise only that it is farther away. Also returns "
                       "a new picture.",
        "parameters": {"type": "object", "properties": {"note": NOTE}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "open_beak",
        "description": "Crouch, open your beak so that what you hold at your beak tip drops on the table just in front of the beak "
                       "tip, and stand up. It does not move you. Returns a new picture.",
        "parameters": {"type": "object", "properties": {"note": NOTE}, "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "done",
        "description": "Tell the person you have finished the command. Call it only when you believe the command is completed.",
        "parameters": {"type": "object", "properties": {"note": {"type": "string", "description": "Optional."}}}}},
]

PERCEPTION_TOOLS = ("look", "find", "measure")  # tools that do not change the world
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)
BOX_ORDERS = ("yxyx", "xyxy")                   # the order of the four numbers of a box the vision model draws (Gemma's native one is y1, x1, y2, x2)
MAX_BOXES = 5
NEAR_REACH = 0.10                               # m: the beak sense reports the banana when its middle is this close to the beak tip
PICTURE_ROWS = (125, 875)                       # the part of the 0..1000 scale that shows the camera's picture when it is padded to a square


def tools_for(find=True, turn=True):
    """The tool list sent to the model: with or without `find`, with or without `turn` (version 12; walk can always turn too)."""
    return [t for t in TOOLS if (find or t["function"]["name"] != "find") and (turn or t["function"]["name"] != "turn")]


# ---- the grasp window (version 10). The benchmark window is the simulator's own: the banana's middle within 5 cm of the beak tip AND its long side
# within 20 degrees of exactly across the beak. The PRACTICE scenes (scenes.py, key `grasp`) use a wider one so that the duck can reach carry and place
# at all; a scene that has no `grasp` entry always gets the benchmark window back. The duck is told the window of the scene it is in (texts_for).
DEFAULT_WINDOW = (duck_sim.GRASP_RADIUS, duck_sim.PICK_ANGLE_TOL)          # (metres, degrees), read once at import: the benchmark window


def grasp_window(scene=None):
    """(radius in m, angle tolerance in degrees) the SIMULATOR uses in a scene: its own `grasp` entry (practice scenes) or the benchmark window.
    The radius is a 3-D distance from the mouth point (a little above the table, and about 3 cm behind the nominal beak tip while the head is down)."""
    g = (scene or {}).get("grasp")
    return (float(g[0]), float(g[1])) if g else DEFAULT_WINDOW


def stated_window(scene=None):
    """(reach in m, angle in degrees) the DUCK IS TOLD in a scene. Third entry of `grasp` = the reach that is true when the banana's middle is measured
    along the table from the beak tip (what the beak sense reports); without it the stated reach is the simulator radius (the benchmark: 5 cm)."""
    g = (scene or {}).get("grasp")
    return (float(g[2]) if g and len(g) > 2 else grasp_window(scene)[0], grasp_window(scene)[1])


def set_grasp_window(radius_m, angle_deg):
    """Make the simulator's grasp rule use this window (duck_sim reads the two constants every time the beak closes)."""
    duck_sim.GRASP_RADIUS, duck_sim.PICK_ANGLE_TOL = float(radius_m), float(angle_deg)


def window_words(window):
    return f"{window[0] * 100:.0f} cm and {window[1]:.0f} degrees"


def texts_for(scene=None, find=True, turn=True):
    """-> (system prompt, tool list) for a scene. The benchmark texts, with the scene's grasp window put into the places that state it (the body facts of the
    system prompt and the close_beak description), so the duck is never told a number that is not true in its scene. Benchmark scenes get the texts unchanged."""
    window, tools = stated_window(scene), tools_for(find, turn)
    if window == DEFAULT_WINDOW:
        return SYSTEM_PROMPT, tools
    old_cm, old_deg = f"within about {DEFAULT_WINDOW[0] * 100:.0f} cm", f"within about {DEFAULT_WINDOW[1]:.0f} degrees"
    new_cm, new_deg = f"within about {window[0] * 100:.0f} cm", f"within about {window[1]:.0f} degrees"
    swap = lambda text: text.replace(old_cm, new_cm).replace(old_deg, new_deg)
    prompt, tools = swap(SYSTEM_PROMPT), copy.deepcopy(tools)
    assert prompt != SYSTEM_PROMPT and old_cm in SYSTEM_PROMPT and old_deg in SYSTEM_PROMPT, "the grasp window is not stated where texts_for() expects it"
    changed = 0
    for t in tools:
        d = t["function"]["description"]
        if swap(d) != d:
            t["function"]["description"] = swap(d)
            changed += 1
    assert changed == 1, "the grasp window must be stated in exactly one tool description (close_beak)"
    return prompt, tools


def body_facts(prompt):
    """The camera and body facts of a system prompt (from "YOUR CAMERA" up to the closing advice): what the duck knew while it acted. The reflection after
    an attempt is given the same text, so that the duck that reads the log knows what the duck that acted knew (for example how wide its picture is)."""
    a, b = prompt.find("YOUR CAMERA"), prompt.find("Think about what you see")
    assert 0 <= a < b, "the camera / body facts are not where body_facts() expects them"
    return prompt[a:b].strip()


def opening_message(command, notebook=None):
    """The first message of an episode: the command a person gives. Nothing about the scene (nothing has been looked at yet).
    `notebook` (learn.py only): the duck's own notes from earlier attempts (franky_memory.Notebook.block()) - written by the duck, not by us."""
    return f"Command: {command}\n\n" + (notebook.strip() + "\n\n" if notebook and notebook.strip() else "") + "You have not looked at anything yet."


def _num(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def describe_spot(p):
    """One measured spot in words: numbers only, no judgement."""
    fwd = f"{p['ahead']:.2f} m ahead" if p["ahead"] >= 0 else f"{-p['ahead']:.2f} m behind you"
    side = f"{abs(p['left']):.2f} m to the {'left' if p['left'] >= 0 else 'right'}"
    b = p["bearing_deg"]
    return f"{fwd}, {side}; distance {p['distance']:.2f} m; bearing {abs(b):.0f} degrees to the {'left' if b >= 0 else 'right'}"


def line_angle(p1, p2):
    """Angle of the line spot1 -> spot2 relative to the direction the duck faces, folded into (-90, 90]. 0 = along, +-90 = across."""
    a = math.degrees(math.atan2(p2["left"] - p1["left"], p2["ahead"] - p1["ahead"]))
    if a > 90.0:
        a -= 180.0
    elif a <= -90.0:
        a += 180.0
    return a


def _json_items(text):
    """Pull the list of detections out of the vision model's answer (plain JSON, JSON in a code fence, or JSON with chatter around it)."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    for cand in (t, t[t.find("["):t.rfind("]") + 1] if "[" in t and "]" in t else "", t[t.find("{"):t.rfind("}") + 1] if "{" in t else ""):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            lists = [v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], (dict, list))]
            return lists[0] if lists and "box_2d" not in obj else [obj]
        if isinstance(obj, list):
            if len(obj) == 4 and all(_num(c) is not None for c in obj):          # a bare [a, b, c, d]: one box
                return [obj]
            return obj
    items = []                                               # last resort: any {...} block or "box_2d": [..] inside the text
    for m in re.finditer(r"\{[^{}]*\}", t):
        try:
            items.append(json.loads(m.group(0)))
        except ValueError:
            pass
    if not items:
        for m in re.finditer(r'box_2d"?\s*[:=]\s*\[([^\]]*)\]', t):
            items.append({"box_2d": [v for v in re.split(r"[,\s]+", m.group(1).strip()) if v]})
    return items


def parse_boxes(text, order="yxyx"):
    """The vision model's answer -> [{"label", "x1", "y1", "x2", "y2"}] on the 0..1000 scale (x across, y down), most confident first.
    `order` says how the model wrote the four numbers: "yxyx" = [y1, x1, y2, x2] (Gemma 4's own order) or "xyxy". Nothing usable -> []."""
    out = []
    for it in _json_items(text):
        label = ""
        if isinstance(it, dict):
            raw = next((it[k] for k in ("box_2d", "bbox_2d", "bbox", "box") if k in it), None)
            label = str(it.get("label") or it.get("name") or "")[:40]
        else:
            raw = it
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        v = [_num(c) for c in raw]
        if any(c is None for c in v):
            continue
        a, b, c, d = v
        y1, x1, y2, x2 = (a, b, c, d) if order == "yxyx" else (b, a, d, c)
        x1, x2, y1, y2 = (max(0.0, min(1000.0, z)) for z in (min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))
        if x2 - x1 >= 1.0 and y2 - y1 >= 1.0:
            out.append({"label": label, "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    return out[:MAX_BOXES]


def footprint(cam, frame, b):
    """(across, deep) in metres: how wide the box is on the table (its bottom corners) and how deep (its top and bottom middle). None if not measurable."""
    xm = (b["x1"] + b["x2"]) / 2
    bl, br = cam.table_point(frame, b["x1"], b["y2"]), cam.table_point(frame, b["x2"], b["y2"])
    top, bot = cam.table_point(frame, xm, b["y1"]), cam.table_point(frame, xm, b["y2"])
    if None in (bl, br, top, bot):
        return None
    return math.hypot(br["ahead"] - bl["ahead"], br["left"] - bl["left"]), abs(top["ahead"] - bot["ahead"])


class ToolResult:
    """What a tool gives back: text, optionally a picture (JPEG bytes), and `meta` for the referee / log (the LLM never sees meta)."""

    def __init__(self, text, image=None, meta=None, caption=None):
        self.text, self.image, self.meta = text, image, meta or {}
        self.caption = caption or "Camera picture."          # the line that goes with the picture in the conversation


# =============================================================================== the tools
class FrankyTools:
    def __init__(self, duck, camera, grounder=None, box_order="yxyx", beak_sense=True, turn_tool=True):
        """grounder(jpeg_bytes, what, box_order) -> (answer text, seconds): the vision model that outlines things for `find`
        (GemmaBrain.ground). None = no `find` tool. beak_sense: a closed beak that did not take the banana reports where the banana was."""
        self.d, self.cam = duck, camera
        self.grounder, self.box_order, self.beak_sense = grounder, box_order, bool(beak_sense)
        self.turn_tool = bool(turn_tool)                              # version 12: the `turn` tool (left / right + positive degrees); walk can always turn as well
        self.reset()

    def reset(self):
        """A new episode: no picture yet, not finished."""
        self.frame = None                 # the latest picture (what `measure` refers to)
        self.yaw0 = self.d.pose()[2]      # the way the body faces now = the zero of the "total turned since the start" line of `walk`
        self.finished = False
        self.triggers = []                # safety events (for the log): [{"guard": ..., "detail": ...}]

    # ------------------------------------------------------------------ helpers
    def _beak(self):
        return ("Beak: holding the banana" if self.d.holding else "Beak: empty") + " (beak tip: 0.10 m ahead of the centre of your body, on the table)."

    def _turned_text(self):
        """The body's own heading since the start of the episode (net: turns to the left minus turns to the right). Plain odometry, no advice."""
        a = math.degrees(math.atan2(math.sin(self.d.pose()[2] - self.yaw0), math.cos(self.d.pose()[2] - self.yaw0)))
        if abs(a) < 1.0:
            return "Total turned since the start: 0 degrees."
        return f"Total turned since the start: {abs(a):.0f} degrees to the {'left' if a > 0 else 'right'}."

    def _picture(self, text, marks=None, meta=None):
        """Take a NEW picture and attach it."""
        self.frame = self.cam.capture()
        return ToolResult(f"{text}\n{self._beak()}\nNew picture #{self.frame.idx} attached.",
                          self.cam.frame_jpeg(self.frame, marks), meta,
                          caption=f"Camera picture #{self.frame.idx} (taken after your last action).")

    def _not_measurable(self, x, y):
        """Why a picture spot has no table point (said in plain words). The grey bars of a square picture are the usual reason."""
        if not self.cam.in_picture(x, y):
            return (f"that spot is in the grey padding, outside what the camera sees (the camera's picture covers y from "
                    f"{PICTURE_ROWS[0]} to {PICTURE_ROWS[1]} on this scale), so it cannot be measured")
        return ("that spot is not on the table surface within range (at or above the horizon, or too far away), so it cannot be "
                "measured")

    def _clip_to_picture(self, b):
        """A vision model sometimes draws a box into the grey bars of a square picture (an object that the camera's picture cuts off at its edge).
        Nothing can be seen there, so the box is cut at the edge of the real picture. -> (box, "lower" | "upper" | "") ("" = not changed)."""
        xc = (b["x1"] + b["x2"]) / 2
        rows = [y for y in range(int(math.floor(b["y1"])), int(math.ceil(b["y2"])) + 1) if self.cam.in_picture(xc, y)]
        if not rows:
            return b, ""
        y1, y2 = max(b["y1"], float(min(rows))), min(b["y2"], float(max(rows)))
        if y2 - y1 < 1.0 or (y1 <= b["y1"] + 0.5 and y2 >= b["y2"] - 0.5):
            return b, ""
        return dict(b, y1=y1, y2=y2), ("lower" if y2 < b["y2"] - 0.5 else "upper")

    def _banana_state(self):
        """The banana as the beak is about to meet it, in the duck's frame: middle (ahead, left) from the body centre and the angle of its
        long side relative to the way the duck faces, folded into (-90, 90] (0 = along, +-90 = across; + = turned toward the left)."""
        d = self.d
        b = d.banana_pos()
        r = d.reach((float(b[0]), float(b[1])))
        a = math.degrees(math.atan2(math.sin(d.banana_axis() - d.pose()[2]), math.cos(d.banana_axis() - d.pose()[2])))
        if a > 90.0:
            a -= 180.0
        elif a <= -90.0:
            a += 180.0
        return r["ahead"], r["left"], a

    def _sense_text(self, before, info):
        """What the beak senses after it closed and did not take the banana. `before` = _banana_state() when the duck began to crouch,
        `info` = the crouch's own record (where the beak tip came down). Raw numbers only."""
        mouth = info.get("mouth_body")
        if not mouth:
            return f"Beak sense: the banana was more than {NEAR_REACH:.2f} m from your beak tip."
        ba, bl, ang = before
        da, dl = ba - mouth[0], bl - mouth[1]                             # banana middle relative to the beak tip
        dist = math.hypot(da, dl)
        if dist > NEAR_REACH:
            return f"Beak sense: the banana was more than {NEAR_REACH:.2f} m from your beak tip."
        return (f"Beak sense: the middle of the banana was {abs(da):.2f} m {'ahead of' if da >= 0 else 'behind'} your beak tip and "
                f"{abs(dl):.2f} m to the {'left' if dl >= 0 else 'right'} of it (distance {dist:.2f} m); its long side points {ang:+.0f} "
                f"degrees from the direction you face (0 = along it, +-90 = across it).")

    def _body_state(self):
        d = self.d
        if d.is_fallen():
            return "You have fallen over and cannot move."
        if d.off_table():
            return "You walked off the table."
        return None

    # ------------------------------------------------------------------ the tools
    def look(self):
        bad = self._body_state()
        if bad:
            return ToolResult(bad)
        return self._picture("You look through your camera.")

    def find(self, what=None, query=None, object=None, name=None, target=None):
        what = next((w for w in (what, query, object, name, target) if w is not None), None)
        what = " ".join(str(what).split())[:80] if what is not None else ""
        if self.frame is None:
            return ToolResult("You have no picture yet. Use look first.")
        if self.grounder is None:
            return ToolResult("The find tool is not available. Use measure.")
        if not what:
            return ToolResult("Bad arguments for find: say what to look for, in a few words (its colour and kind).")
        frame = self.frame
        try:
            raw, secs = self.grounder(self.cam.frame_jpeg(frame), what, self.box_order)
        except Exception as e:                                           # Ollama hiccup: the agent can try again or use measure
            return ToolResult(f"The vision model could not be reached ({str(e)[:100]}). Try again, or use measure.",
                              meta={"find": what, "spots": [], "grounding": {"what": what, "raw": f"ERROR {e}", "llm_s": 0.0, "boxes": []}})
        boxes = parse_boxes(raw, self.box_order)
        g = {"what": what, "raw": (raw or "")[:800], "llm_s": round(secs, 2), "boxes": boxes}
        if not boxes:
            return ToolResult(f'Find "{what}" on picture #{frame.idx}: the vision model returned no box, so it does not see anything '
                              f"like that in the picture (or could not outline it).\n{self._beak()}",
                              meta={"find": what, "spots": [], "grounding": g})
        lines = [f'Found {len(boxes)} match{"es" if len(boxes) > 1 else ""} for "{what}" on picture #{frame.idx}:']
        spots, drawn = [], []
        for i, b in enumerate(boxes, 1):
            b, cut = self._clip_to_picture(b)
            xc, yc = (b["x1"] + b["x2"]) / 2, (b["y1"] + b["y2"]) / 2
            lab = f' ("{b["label"]}")' if b["label"] else ""
            lines.append(f"box {i}{lab}: picture x {b['x1']:.0f} to {b['x2']:.0f}, y {b['y1']:.0f} to {b['y2']:.0f}.")
            if cut:
                lines.append(f"   (the vision model's box reached into the grey padding, so it was cut at the {cut} edge of the camera's picture: "
                             + ("the object may go on below it, in the strip right below your beak that the camera cannot see)"
                                if cut == "lower" else "the object may go on above it)"))
            mid = self.cam.table_point(frame, xc, yc)
            if mid is None:
                lines.append("   its middle: " + self._not_measurable(xc, yc) + ".")
            else:
                lines.append(f"   middle of the box on the table: {describe_spot(mid)}.")
                fp = footprint(self.cam, frame, b)
                if fp:
                    lines.append(f"   footprint on the table: about {fp[0]:.2f} m across (left-right) by {fp[1]:.2f} m deep (front-back).")
            spots.append({"x": xc, "y": yc, "world": mid["world"] if mid else None})
            drawn.append((b["x1"], b["y1"], b["x2"], b["y2"], i))
        lines.append("The picture below has the boxes drawn and numbered. A vision model drew them: check that they are on the right thing.")
        return ToolResult("\n".join(lines) + f"\n{self._beak()}", self.cam.frame_jpeg(frame, boxes=drawn),
                          {"find": what, "spots": spots, "grounding": g},
                          caption=f"Camera picture #{frame.idx} again, with the boxes found for \"{what}\" drawn on it.")

    def measure(self, x=None, y=None, x2=None, y2=None, x1=None, y1=None):
        x = _num(x if x is not None else x1)
        y = _num(y if y is not None else y1)
        x2, y2 = _num(x2), _num(y2)
        if self.frame is None:
            return ToolResult("You have no picture yet. Use look first.")
        if x is None or y is None:
            return ToolResult("Bad arguments for measure: x and y must be numbers from 0 to 1000 (the spot in the picture).")
        spots = [(x, y)]
        if x2 is not None and y2 is not None:
            spots.append((x2, y2))
        elif (x2 is None) != (y2 is None):
            return ToolResult("Bad arguments for measure: give both x2 and y2 for a second spot, or neither.")
        if any(not (0.0 <= sx <= 1000.0 and 0.0 <= sy <= 1000.0) for sx, sy in spots):
            return ToolResult("Bad arguments for measure: x and y must be between 0 and 1000 (0 = left / top edge of the picture, 1000 = right / bottom edge).")
        pts = [self.cam.table_point(self.frame, sx, sy) for sx, sy in spots]
        marks = [(sx, sy, i + 1) for i, (sx, sy) in enumerate(spots)]
        lines = [f"Measured on picture #{self.frame.idx}:"]
        for i, ((sx, sy), p) in enumerate(zip(spots, pts), 1):
            if p is None:
                lines.append(f"spot {i} (x={sx:.0f}, y={sy:.0f}): {self._not_measurable(sx, sy)}.")
            else:
                lines.append(f"spot {i} (x={sx:.0f}, y={sy:.0f}): {describe_spot(p)}.")
        if len(pts) == 2 and all(p is not None for p in pts):
            p1, p2 = pts
            mid = {"ahead": (p1["ahead"] + p2["ahead"]) / 2, "left": (p1["left"] + p2["left"]) / 2}
            mid["distance"] = math.hypot(mid["ahead"], mid["left"])
            mid["bearing_deg"] = math.degrees(math.atan2(mid["left"], mid["ahead"]))
            length = math.hypot(p2["ahead"] - p1["ahead"], p2["left"] - p1["left"])
            ang = line_angle(p1, p2)
            lines.append(f"middle between the spots: {describe_spot(mid)}.")
            lines.append(f"line from spot 1 to spot 2: length {length:.3f} m; angle {ang:+.0f} degrees relative to the direction you face "
                         f"(0 = along it, +-90 = across it).")
        lines.append("The picture below has your spots marked with red crosses.")
        meta = {"spots": [{"x": sx, "y": sy, "world": (p["world"] if p else None)} for (sx, sy), p in zip(spots, pts)]}
        return ToolResult("\n".join(lines) + f"\n{self._beak()}", self.cam.frame_jpeg(self.frame, marks), meta,
                          caption=f"Camera picture #{self.frame.idx} again, with your measured spots marked by red crosses.")

    def turn(self, direction="", degrees=0.0):
        """Version 12: turn on the spot to the left or right by a positive number of degrees = walk(forward=0, turn=+-degrees), with the answer in the words of this tool."""
        word = str(direction).strip().lower()
        if word not in ("left", "right"):
            return ToolResult("Bad arguments for turn: direction must be \"left\" or \"right\". You did not move.")
        deg = _num(degrees, None)
        if deg is None or deg < 0:
            return ToolResult("Bad arguments for turn: degrees must be a number from 0 to 180 (never negative: say left or right in direction). You did not move.")
        deg = min(deg, motion.MAX_TURN)
        return self.walk(0.0, deg if word == "left" else -deg, _asked=f"turn {deg:.0f} degrees to the {word}")

    def walk(self, forward=0.0, turn=0.0, _asked=None):
        d = self.d
        bad = self._body_state()
        if bad:
            return ToolResult(bad)
        fwd, trn = _num(forward, 0.0), _num(turn, 0.0)
        notes = []
        cf = max(-motion.MAX_BACK, min(motion.MAX_FORWARD, fwd))
        ct = max(-motion.MAX_TURN, min(motion.MAX_TURN, trn))
        if cf != fwd:
            notes.append(f"forward was limited to {cf:+.2f} m")
        if ct != trn:
            notes.append(f"turn was limited to {ct:+.0f} degrees")
        fwd, trn = cf, ct
        if abs(fwd) <= 0.01 and abs(trn) < motion.IGNORE_TURN:
            return ToolResult("Nothing to do: " + ("you asked for (almost) 0 degrees, so you did not move." if _asked else "forward and turn are both (almost) 0, so you did not move."),
                              meta={"asked": (fwd, trn), "did": (0.0, 0.0), "refused": False, "stopped": None})
        why = motion.refusal(d, fwd, trn)
        if why:
            self.triggers.append({"guard": "walk_refused", "detail": why})
            lim = (" Note: " + "; ".join(notes) + ".") if notes else ""
            return ToolResult(f"Refused for safety: {why}. You did not move.{lim}\n{self._beak()}",
                              meta={"asked": (fwd, trn), "did": (0.0, 0.0), "refused": True, "stopped": None})
        r = motion.drive(d, fwd, trn)
        if r["stopped"]:
            self.triggers.append({"guard": "walk_stopped", "detail": r["stopped"]})
        if r["fell"]:
            return ToolResult("You fell over while moving.", meta={"asked": (fwd, trn), "did": (r["moved_m"], r["turned_deg"]), "fell": True})
        if r["off_table"]:
            return ToolResult("You walked off the table.", meta={"asked": (fwd, trn), "did": (r["moved_m"], r["turned_deg"]), "off_table": True})
        mv = (f"moved forward {r['moved_m']:.2f} m" if r["moved_m"] >= 0.01 else
              f"moved backward {-r['moved_m']:.2f} m" if r["moved_m"] <= -0.01 else "did not move forward or backward")
        tn = (f"turned {abs(r['turned_deg']):.0f} degrees to the {'left' if r['turned_deg'] > 0 else 'right'}"
              if abs(r["turned_deg"]) >= 1.0 else "did not turn")
        asked = _asked or f"turn {trn:+.0f} degrees, forward {fwd:+.2f} m"
        text = f"Your body {tn} and {mv} (you asked: {asked}). {self._turned_text()}"
        if r["stopped"]:
            text += f" It stopped early: {r['stopped']}."
        if motion.IGNORE_TURN <= abs(trn) < motion.TURN_IN_PLACE_MIN and abs(fwd) < motion.ARC_MIN_FORWARD:
            text += (" (A turn this small on the spot does not work well: turn 30 degrees or more, or walk at least 0.10 m forward while you turn.)" if not _asked else
                     " (A turn this small on the spot does not work well: turn 30 degrees or more, or use walk with a forward distance of at least 0.10 m while you turn.)")
        if notes:
            text += " Note: " + "; ".join(notes) + "."
        return self._picture(text, meta={"asked": (fwd, trn), "did": (r["moved_m"], r["turned_deg"]), "refused": False,
                                         "stopped": r["stopped"]})

    def pick(self):
        d = self.d
        bad = self._body_state()
        if bad:
            return ToolResult(bad)
        if d.holding:
            return ToolResult("You are already holding the banana, so there is nothing to pick up. You did not crouch.")
        before = self._banana_state()
        info = d.ground_pick(grasp=True)
        if d.is_fallen():
            return ToolResult("You fell over while crouching.", meta={"grasped": False, "fell": True})
        sense = {"ahead": before[0], "left": before[1], "angle": before[2]}
        if info["grasped"]:
            # the same numbers as after a failed close, so that a success and a failure can be compared (version 6: a success used to say nothing,
            # and the duck wrote down a wrong reason for the only grasp it ever made)
            text = "You crouched and closed your beak: it closed on the banana. You are now holding it."
            return self._picture(text + ("\n" + self._sense_text(before, info) if self.beak_sense else ""), meta={"grasped": True, "sense": sense})
        if not self.beak_sense:                                         # the version-4 message: it does not say which of the two it was
            return self._picture("You crouched and closed your beak: it closed on nothing (the banana was not under your beak tip, "
                                 "or not across your beak).", meta={"grasped": False})
        return self._picture("You crouched and closed your beak: it did NOT take the banana. The banana is still on the table and your "
                             "beak is empty.\n" + self._sense_text(before, info), meta={"grasped": False, "sense": sense})

    def place(self):
        d = self.d
        bad = self._body_state()
        if bad:
            return ToolResult(bad)
        if not d.holding:
            return ToolResult("You are not holding anything, so there is nothing to let go of. You did not crouch.")
        d.ground_pick(release=True)
        d.hold(1.5)                                    # let the banana settle
        if d.is_fallen():
            return ToolResult("You fell over while crouching.", meta={"released": False, "fell": True})
        return self._picture("You crouched and opened your beak: you let go of the banana at your beak tip.", meta={"released": True})

    def done(self):
        self.finished = True
        return ToolResult("Finished: you told the person the command is completed.")

    def call(self, name, args):
        method = {"close_beak": "pick", "open_beak": "place"}.get(name, name)        # the methods keep their old names; "pick" / "place" still work
        if (name not in TOOL_NAMES and name not in ("pick", "place")) or (name == "turn" and not self.turn_tool):
            names = [n for n in TOOL_NAMES if n != "turn" or self.turn_tool]
            return ToolResult(f"Unknown tool '{name}'. Tools: {', '.join(names)}." + ("" if self.turn_tool else " (To turn, give walk a turn number.)"))
        args = {k: v for k, v in (args or {}).items() if k != "note"}          # the note is only for the history
        try:
            return getattr(self, method)(**args)
        except TypeError as e:
            return ToolResult(f"Bad arguments for {name}: {e}")
