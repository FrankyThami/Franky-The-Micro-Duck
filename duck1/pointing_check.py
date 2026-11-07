"""pointing_check.py - can Gemma LOCATE the banana and the green box in the real camera picture? Run this BEFORE the scenes.

    python duck1/pointing_check.py                      # the `find` way (Gemma draws boxes), 4:3 picture AND square picture    (about 2 minutes)
    python duck1/pointing_check.py --image-mode native  # only the normal 4:3 picture
    python duck1/pointing_check.py --method point       # the `measure` way (Gemma points at pixels through a tool call)
    python duck1/pointing_check.py --model gemma4:e4b   # another model
    python duck1/pointing_check.py --scenes 0 3 4       # only some scenes

The whole agent stands on one skill: locating things in the picture. If the model cannot do that, no amount of planning helps, and
a full run only tells you "it failed" without telling you why. This script tests ONLY that skill. For every scene the duck stands at
its start position, takes ONE picture, and the model is asked to locate (1) the banana and (2) the green box. The answer is scored
against the TRUE positions (the model never sees them). Targets that are not in the picture are skipped.

  --method find   (default) the model is asked, with the same question the `find` tool uses, for a box around "banana" / "green box"
                  in Gemma 4's native detection format. The four numbers are read BOTH as [y1, x1, y2, x2] and as [x1, y1, x2, y2], and
                  both are scored: the one that fits tells you which --box-order is right for your Ollama.
                  Score: how far the box's middle is from the true object (cm on the table; 0 = on it), the box overlap (IoU) with the
                  true outline in the picture, and the offset of the box middle from the true middle in picture units (x = across,
                  y = down; 0 = exact) - a model that is only shifted in y shows a big y offset and a small x offset.
  --image-mode    native = the 4:3 camera picture as it is. square = the same picture on a square canvas with grey bars above and below
                  (the benchmark's --pad-square on). both (default) runs both on the same pictures, so you can see if the shape matters.
                  First real run (gemma4:e2b, native): x about right, y 100-260 units too low for far things.
  --method point  the model points at the two ends of the banana / the middle of the box with the `measure` tool (the first version of this test).

What the numbers mean for the agent: to pick the banana it has to come within 5 cm of its middle and within 20 degrees of crosswise.
Boxes only need to be about right at the start (the duck walks closer and looks again), but if the median error is more than ~5 cm even
for the banana, expect many missed grabs.

Output: a table, and the pictures with the model's boxes / crosses, in  outputs\\logs\\duck1\\pointing_<time>\\ .
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path

import argparse
import base64
import json
import math
import os
import statistics
import time

from agent_harness.gemma_brain import GemmaBrain
from agent_harness.referee import BANANA_HALF, BOX_HALF, ON_OBJECT_CM, Referee
from agent_harness.run_harness import truth
from duck1 import scenes
from duck1.franky_tools import BOX_ORDERS, FrankyTools, SYSTEM_PROMPT, TOOLS, footprint, parse_boxes
from sim.duck_sim import Duck, TABLE_H
from sim.head_camera import HeadCamera
from project_paths import log_dir

BANANA_Q = ("Test question. The picture is what your camera sees. Use the measure tool on the banana: point at its two ends "
            "(x, y and x2, y2). Call measure now.")
BOX_Q = ("Test question. The picture is what your camera sees. Use the measure tool on the green box: point at the middle of the box. "
         "Call measure now.")
WHAT = {"banana": "banana", "box": "green box"}          # the words used with the vision model (the same kind of words the agent uses)
BOX_MIDDLE_CM = 0.5                                      # a box middle counts as "on the green box" when the spot error is below this


def visible(cam, frame, points, margin=40):
    for p in points:
        px = cam.pixel_of_world(frame, *p)
        if px is None or not cam.in_picture(px[0], px[1], margin):
            return False
    return True


def true_bbox(cam, frame, duck, target):
    """The true outline of the banana / the box in the picture as (x1, y1, x2, y2) on the 0..1000 scale of what the model sees,
    or None if a corner is behind the camera. (Parts outside the picture are kept: the model sees a cut-off object.)"""
    pts = []
    if target == "banana":
        b, ax = duck.banana_pos(), duck.banana_axis()
        ux, uy = math.cos(ax), math.sin(ax)
        for s in (1, -1):
            for p in (1, -1):
                for z in (0.0, 0.018):
                    pts.append((b[0] + s * BANANA_HALF * ux - p * 0.009 * uy, b[1] + s * BANANA_HALF * uy + p * 0.009 * ux, TABLE_H + z))
    else:
        g = duck.box_pos()
        for sx in (1, -1):
            for sy in (1, -1):
                for z in (0.0, 0.016):
                    pts.append((g[0] + sx * BOX_HALF, g[1] + sy * BOX_HALF, TABLE_H + z))
    px = [cam.pixel_of_world(frame, *p) for p in pts]
    if any(p is None for p in px):
        return None
    xs, ys = [p[0] for p in px], [p[1] for p in px]
    return min(xs), min(ys), max(xs), max(ys)


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def lying_deg(duck):
    """The angle between the TRUE banana and the way the duck faces, 0..90: 90 = across (what the beak needs), 0 = pointing away from the duck."""
    a = math.degrees(duck.banana_axis() - duck.pose()[2])
    return abs(((a + 90) % 180) - 90)


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def fmt(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--method", choices=["find", "point", "both"], default="find")
    ap.add_argument("--image-mode", choices=["native", "square", "both"], default="both",
                    help="native = the 4:3 picture, square = padded to a square with grey bars (default: both, on the same pictures)")
    ap.add_argument("--box-order", choices=list(BOX_ORDERS), default="yxyx",
                    help="how the model is ASKED to write a box (both orders are scored whatever you choose)")
    ap.add_argument("--think", choices=["on", "off"], default="on", help="only for --method point (the find question never uses thinking)")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--scenes", type=int, nargs="*", default=None)
    ap.add_argument("--logdir", default=log_dir("duck1"))
    args = ap.parse_args()
    ids = list(range(scenes.N_BENCHMARK)) if args.scenes is None else args.scenes
    do_find, do_point = args.method in ("find", "both"), args.method in ("point", "both")
    modes = ["native", "square"] if args.image_mode == "both" else [args.image_mode]
    brain = GemmaBrain(args.model, args.host, num_ctx=args.num_ctx, think=args.think == "on", keep_images=1)
    print(f"Loading {args.model}...", flush=True)
    try:
        for w in brain.check():
            print("  note:", w)
        print(f"  ready after {brain.warmup():.0f} s", flush=True)
    except Exception as e:
        raise SystemExit(f"Cannot use the model: {e}")
    out = os.path.join(args.logdir, f"pointing_{time.strftime('%Y%m%d_%H%M%S')}_{args.model.replace(':', '-')}")
    os.makedirs(out, exist_ok=True)
    measure_only = [t for t in TOOLS if t["function"]["name"] == "measure"]

    duck = Duck("Franky", viewer=False)
    plain = duck.reset

    def reset_scene(seed=0, **kw):
        sc = scenes.SCENES[seed]
        return plain(seed=seed, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
                     banana_xy=sc["banana"], box_xy=sc["box"], banana_yaw=math.radians(sc.get("banana_yaw", 0.0)))

    cam = HeadCamera(duck)
    tools = FrankyTools(duck, cam)
    ref = Referee(duck, truth)
    rows, log = [], []
    try:
        for sid in ids:
            reset_scene(seed=sid)
            tools.reset()
            cam.pad_square = False
            first = tools.look()
            f = tools.frame
            b, g = duck.banana_pos(), duck.box_pos()
            ax = duck.banana_axis()
            ends = [(b[0] + s * BANANA_HALF * math.cos(ax), b[1] + s * BANANA_HALF * math.sin(ax), TABLE_H + 0.009) for s in (1, -1)]
            vis = {"banana": visible(cam, f, ends), "box": visible(cam, f, [(g[0], g[1], TABLE_H + 0.01)])}
            for target in ("banana", "box"):
                if not vis[target]:
                    rows.append({"scene": sid, "target": target, "method": "-", "visible": False})
                    print(f"scene {sid} {target:<7} not in the picture - skipped")
                    continue
                # ------------------------------------------------------------ find: native boxes, in every image mode
                if do_find:
                    for mode in modes:
                        cam.pad_square = mode == "square"
                        row = {"scene": sid, "target": target, "method": "find", "mode": mode, "visible": True}
                        jpeg = cam.frame_jpeg(f)
                        t0 = time.time()
                        try:
                            raw, _ = brain.ground(jpeg, WHAT[target], args.box_order)
                        except Exception as e:
                            row["error"] = str(e)
                            rows.append(row)
                            print(f"scene {sid} {target:<7} find/{mode}: LLM error: {e}")
                            continue
                        row["llm_s"] = round(time.time() - t0, 1)
                        row["raw"] = raw
                        tb = true_bbox(cam, f, duck, target)
                        drawn, parts = [], []
                        for order in BOX_ORDERS:
                            boxes = parse_boxes(raw, order)
                            if not boxes:
                                row[order] = None
                                parts.append(f"{order}: no box")
                                continue
                            bx = boxes[0]
                            xc, yc = (bx["x1"] + bx["x2"]) / 2, (bx["y1"] + bx["y2"]) / 2
                            mid = cam.table_point(f, xc, yc)
                            err = ref.spot_error(target, mid["world"]) if mid else None
                            fp = footprint(cam, f, bx) if mid else None
                            ov = iou((bx["x1"], bx["y1"], bx["x2"], bx["y2"]), tb) if tb else None
                            dx = None if tb is None else xc - (tb[0] + tb[2]) / 2
                            dy = None if tb is None else yc - (tb[1] + tb[3]) / 2
                            row[order] = {"box": bx, "err_cm": None if err is None else round(err, 1), "iou": None if ov is None else round(ov, 2),
                                          "dx": None if dx is None else round(dx), "dy": None if dy is None else round(dy),
                                          "footprint_m": None if fp is None else [round(fp[0], 3), round(fp[1], 3)], "n_boxes": len(boxes)}
                            drawn.append((bx["x1"], bx["y1"], bx["x2"], bx["y2"], order[:2]))
                            parts.append(f"{order}: {fmt(err)} cm off (dx {fmt(dx, 0)}, dy {fmt(dy, 0)}), IoU {fmt(ov, 2)}"
                                         + (f", footprint {100 * fp[0]:.0f}x{100 * fp[1]:.0f} cm" if (fp and target == "banana") else ""))
                        with open(os.path.join(out, f"s{sid:02d}_{target}_find_{mode}.jpg"), "wb") as fh:
                            fh.write(cam.frame_jpeg(f, boxes=drawn))
                        rows.append(row)
                        log.append({"scene": sid, "target": target, "method": "find", "mode": mode, "raw": raw, "row": row})
                        print(f"scene {sid} {target:<7} find/{mode:<6} | " + " | ".join(parts) + f"   ({row['llm_s']} s)")
                    cam.pad_square = False
                # ------------------------------------------------------------ point: measure through a tool call (4:3 picture)
                if do_point:
                    cam.pad_square = False
                    row = {"scene": sid, "target": target, "method": "point", "mode": "native", "visible": True}
                    tools.frame = f
                    question = BANANA_Q if target == "banana" else BOX_Q
                    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Command: " + question + "\n\nYou have not moved."},
                            {"role": "user", "content": "Camera picture #1.", "images": [base64.b64encode(first.image).decode()]}]
                    brain.new_episode()
                    t0 = time.time()
                    try:
                        resp = brain.chat(msgs, measure_only)
                    except Exception as e:
                        row["error"] = str(e)
                        rows.append(row)
                        print(f"scene {sid} {target:<7} point: LLM error: {e}")
                        continue
                    m = resp.get("message", {})
                    calls = m.get("tool_calls") or []
                    row["llm_s"] = round(time.time() - t0, 1)
                    if not calls or calls[0]["function"]["name"] != "measure":
                        row["error"] = "no measure call"
                        rows.append(row)
                        print(f"scene {sid} {target:<7} point: the model did not call measure: {(m.get('content') or '')[:80]!r}")
                        continue
                    a = calls[0]["function"].get("arguments") or {}
                    if isinstance(a, str):
                        a = json.loads(a)
                    res = tools.call("measure", a)
                    spots = res.meta.get("spots", [])
                    errs = [ref.spot_error(target, s["world"]) if s.get("world") else None for s in spots]
                    row.update(args={k: v for k, v in a.items() if k != "note"}, errs=errs)
                    if res.image:
                        with open(os.path.join(out, f"s{sid:02d}_{target}_point.jpg"), "wb") as fh:
                            fh.write(res.image)
                    if target == "banana" and len(spots) == 2 and all(s.get("world") for s in spots):
                        (x1, y1), (x2, y2) = spots[0]["world"], spots[1]["world"]
                        mid = ((x1 + x2) / 2, (y1 + y2) / 2)
                        row["mid_err_cm"] = round(100 * math.hypot(mid[0] - b[0], mid[1] - b[1]), 1)
                        ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) - math.degrees(ax)
                        row["angle_err_deg"] = round(abs((ang + 90) % 180 - 90), 0)
                    elif target == "banana":
                        row["note"] = "needs two points"
                    rows.append(row)
                    log.append({"scene": sid, "target": target, "method": "point", "thinking": m.get("thinking"), "message": m, "row": row})
                    es = ", ".join("n/a" if e is None else f"{e:.1f}" for e in errs)
                    extra = f"  middle off by {row['mid_err_cm']} cm, angle off by {row['angle_err_deg']:.0f} deg" if "mid_err_cm" in row else ""
                    print(f"scene {sid} {target:<7} point | spot errors (cm): {es}{extra}   ({row['llm_s']} s)  args {row['args']}")
    finally:
        cam.close()
        duck.close()
        with open(os.path.join(out, "pointing.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": args.model, "method": args.method, "ask_order": args.box_order, "rows": rows, "log": log}, fh, indent=2, default=str)

    print("\n" + "=" * 110)
    if do_find:
        print(f"FIND ({args.model}; native boxes, the model was asked for {args.box_order}). Error = distance of the box middle from the true object, cm (0 = on it).")
        medians = {}
        for mode in modes:
            for order in BOX_ORDERS:
                for target in ("banana", "box"):
                    rs = [r for r in rows if r["target"] == target and r.get("method") == "find" and r.get("mode") == mode and r.get(order)]
                    errs = [r[order]["err_cm"] for r in rs]
                    ious = [r[order]["iou"] for r in rs]
                    dxs, dys = [r[order]["dx"] for r in rs], [r[order]["dy"] for r in rs]
                    n_vis = sum(1 for r in rows if r["target"] == target and r.get("method") == "find" and r.get("mode") == mode)
                    lim = ON_OBJECT_CM if target == "banana" else BOX_MIDDLE_CM
                    good = sum(1 for e in errs if e is not None and e <= lim)
                    medians[(mode, order, target)] = med(errs)
                    what = f"within {ON_OBJECT_CM:.0f} cm of the banana" if target == "banana" else "on the green box"
                    print(f"  {mode:<6} read as {order}  {target:<7}: boxes {len(rs)}/{n_vis}   middle {what}: {good}/{len(rs)}   median error {fmt(med(errs))} cm"
                          f"   median IoU {fmt(med(ious), 2)}   median offset x {fmt(med(dxs), 0)}, y {fmt(med(dys), 0)}")
        combos = [(m, o) for m in modes for o in BOX_ORDERS]
        have = {c: [medians[(c[0], c[1], t)] for t in ("banana", "box") if medians.get((c[0], c[1], t)) is not None] for c in combos}
        have = {c: v for c, v in have.items() if len(v) == 2}
        if have:
            best = min(have, key=lambda c: sum(have[c]))
            print(f"  -> best fit: {best[0]} picture, boxes read as {best[1]}   (banana {have[best][0]:.1f} cm, box {have[best][1]:.1f} cm median error)")
            flags = []
            if best[1] != args.box_order:
                flags.append(f"--box-order {best[1]}")
            if best[0] == "square":
                flags.append("--pad-square on")
            print("  -> run the benchmark with:  " + (" ".join(flags) if flags else "the defaults (nothing to add)"))
    if do_point:
        for target in ("banana", "box"):
            rs = [r for r in rows if r["target"] == target and r.get("method") == "point" and r.get("errs")]
            spots = [e for r in rs for e in r["errs"]]
            on = [e for e in spots if e is not None and e <= ON_OBJECT_CM]
            n_vis = sum(1 for r in rows if r["target"] == target and r.get("method") == "point")
            print(f"POINT {target:<7}: tried {len(rs)}/{n_vis} visible   spots on the object: {len(on)}/{len(spots)}"
                  + (f"   median spot error {statistics.median(e for e in spots if e is not None):.1f} cm" if any(e is not None for e in spots) else ""))
            if target == "banana":
                mids = [r["mid_err_cm"] for r in rs if "mid_err_cm" in r]
                angs = [r["angle_err_deg"] for r in rs if "angle_err_deg" in r]
                if mids:
                    print(f"         banana middle: median {statistics.median(mids):.1f} cm off, worst {max(mids):.1f}   |   angle: median {statistics.median(angs):.0f} deg off, worst {max(angs):.0f}")
    print("=" * 110)
    print("pictures with the model's boxes / crosses:", out)


if __name__ == "__main__":
    main()
