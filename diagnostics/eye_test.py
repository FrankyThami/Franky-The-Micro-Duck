"""eye_test.py - STEP 2: does the eye FIND the banana and the box, and the banana's angle?

The eye (eye.py) uses only the camera picture. THIS TEST compares what it says with the true positions from the
simulator - the truth is used for the comparison only, the duck never sees it.

    python eye_test.py
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import math

import mujoco

from duck1 import scenes
from sim.duck_sim import Duck
from sim.eye import Eye


def fold180(a_deg):
    """angle of an undirected line, folded into [-90, 90)"""
    return (a_deg + 90.0) % 180.0 - 90.0


def set_scene(duck, sc, banana_yaw_deg=0.0):
    duck.reset(seed=0, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
               banana_xy=sc["banana"], box_xy=sc["box"])
    y = math.radians(banana_yaw_deg)                       # lay the banana along this world direction
    duck.data.qpos[duck.ban_q + 3:duck.ban_q + 7] = [math.cos(y / 2), 0.0, 0.0, math.sin(y / 2)]
    mujoco.mj_forward(duck.model, duck.data)


def main():
    duck = Duck("Franky", viewer=False, realtime=False)
    eye = Eye(duck)
    pos_err, box_err = [], []

    print("PART 1 - the 10 scenes (banana lying along the table, like before)")
    print(f"{'scene':<18}{'banana: eye says (ahead, left)':<34}{'true':<18}{'error':>8}   {'box: eye says':<20}{'true':<18}{'error':>8}")
    for sid, sc in enumerate(scenes.SCENES):
        set_scene(duck, sc)
        seen = eye.look()
        tb, tg = duck.reach(duck.banana_pos()[:2]), duck.reach(duck.box_pos()[:2])
        b, g = seen["banana"], seen["box"]
        if b:
            e = 100 * math.hypot(b["ahead"] - tb["ahead"], b["left"] - tb["left"])
            pos_err.append(e)
            bt = f"({b['ahead']:+.2f}, {b['left']:+.2f})"
            be = f"{e:.1f} cm"
        else:
            bt, be = "not seen", "-"
        if g:
            e2 = 100 * math.hypot(g["ahead"] - tg["ahead"], g["left"] - tg["left"])
            box_err.append(e2)
            gt = f"({g['ahead']:+.2f}, {g['left']:+.2f})"
            ge = f"{e2:.1f} cm"
        else:
            gt, ge = "not seen", "-"
        tbs = f"({tb['ahead']:+.2f}, {tb['left']:+.2f})"
        tgs = f"({tg['ahead']:+.2f}, {tg['left']:+.2f})"
        print(f"{sid} {sc['name']:<16}{bt:<34}{tbs:<18}{be:>8}   {gt:<20}{tgs:<18}{ge:>8}")

    print("\nPART 2 - the banana's ANGLE: banana turned to 6 different directions, seen from 3 distances")
    print("(angle = direction of the banana's long side relative to the duck's beak: 0 = along the beak, +-90 = across)")
    ang_err = []
    for label, sc in (("0.60 m away", dict(banana=(0.20, 0.12), box=(0.20, -0.20))),
                      ("0.40 m away", dict(banana=(0.20, 0.12), box=(0.20, -0.20), duck=(-0.20, 0.0))),
                      ("0.28 m away", dict(banana=(0.20, 0.12), box=(0.20, -0.20), duck=(-0.08, 0.0)))):
        row = []
        for yaw in (0, 30, 60, 90, 120, 150):
            set_scene(duck, sc, yaw)
            b = eye.look()["banana"]
            true = fold180(yaw - math.degrees(duck.pose()[2]))
            if b and b["angle_deg"] is not None:
                err = abs(fold180(b["angle_deg"] - true))
                ang_err.append(err)
                row.append(f"{err:4.1f}")
            else:
                row.append(" n/a")
        print(f"  banana {label}: angle error in degrees for banana turned to 0/30/60/90/120/150: " + "  ".join(row))

    print("\nSUMMARY")
    if pos_err:
        print(f"  banana position error: mean {sum(pos_err) / len(pos_err):.1f} cm, worst {max(pos_err):.1f} cm  ({len(pos_err)} of 10 scenes seen)")
    if box_err:
        print(f"  box position error:    mean {sum(box_err) / len(box_err):.1f} cm, worst {max(box_err):.1f} cm  ({len(box_err)} of 10 scenes seen)")
    if ang_err:
        print(f"  banana angle error:    mean {sum(ang_err) / len(ang_err):.1f} deg, worst {max(ang_err):.1f} deg")
    eye.close()
    duck.close()


if __name__ == "__main__":
    main()