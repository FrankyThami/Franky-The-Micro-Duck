"""body_test.py (v3) - tests the BODY skills of duck_sim.py (no camera, no LLM): the crosswise-grip rule for picking,
approach_banana() and back_up().  The skills are given the TRUE banana position here, on purpose: this only tests
that the body can do what it is asked.   Run:  python body_test.py

The duck holds the banana CROSSWISE (across the beak, like a bar across its face). "err" below = how far the banana
is from lying exactly across the beak: 0 = perfect, 90 = lying along the beak. A pick works only if err <= PICK_ANGLE_TOL.
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import math

from duck1 import scenes
from sim.duck_sim import PICK_ANGLE_TOL, Duck

d = Duck("Franky")


def reset(sc, banana_yaw_deg=0.0):
    d.reset(seed=0, duck_xy=sc.get("duck", (-0.4, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
            banana_xy=sc["banana"], box_xy=sc["box"], banana_yaw=math.radians(banana_yaw_deg))


print(f"PART 1 - the rule: a pick only works if the banana lies ACROSS the beak (within {PICK_ANGLE_TOL:.0f} deg)")
for label, byaw in (("banana lies along the beak ", 0.0), ("banana lies across the beak", 90.0)):
    reset(dict(banana=(-0.30, 0.0), box=(0.2, -0.2)), byaw)         # 10 cm in front of the beak: same spot both times
    d.walk(0.4, 0, 0, 0.4); d.settle()
    p = d.ground_pick(grasp=True)
    print(f"  {label}: err = {p['angle_err_deg']:5.1f} deg, distance = {p['min_dist']:.3f} m -> picked = {p['grasped']}")

print("\nPART 1b - what the duck holds: the banana must hang ACROSS the beak, its middle on the beak tip")
reset(dict(banana=(-0.30, 0.0), box=(0.2, -0.2)), 90.0)
d.walk(0.4, 0, 0, 0.4); d.settle()
p = d.ground_pick(grasp=True)
d.hold(0.5)
rel = math.degrees(d._wrap(d.banana_axis() - d.pose()[2]))
mouth = d.data.site_xpos[d.mouth_id]
print(f"  held banana direction relative to the beak = {rel:+.1f} deg (should be +-90), "
      f"middle {abs(float((d.banana_pos() - mouth)[0])) * 100:.1f} cm from the mouth tip -> holding = {d.holding}")

print("\nPART 2 - the 10 scenes: approach_banana() then pick (the skill is told where the banana is and which way it lies)")
ok2 = 0
for sid, sc in enumerate(scenes.SCENES):
    reset(sc)
    note = ""
    if sid == 5:                                                     # banana only 4 cm in front of the feet
        r = d.back_up()
        note = f"back_up {r['moved']:.2f} m; "
    d.approach_banana(d.banana_pos()[:2], d.banana_axis(), box_xy=d.box_pos()[:2]); d.settle()
    p = d.ground_pick(grasp=True)
    ok2 += p["grasped"]
    print(f"  scene {sid} {sc['name']:<14} {note}err = {p['angle_err_deg']:5.1f} deg, distance = {p['min_dist']:.3f} m -> picked = {p['grasped']}")
print(f"  => picked {ok2}/10")

print("\nPART 3 - the same, but the banana lies at other angles (35, -50, 80 deg)")
ok3 = n3 = 0
for sid in (0, 1, 3, 7):
    sc = scenes.SCENES[sid]
    row = []
    for byaw in (35.0, -50.0, 80.0):
        reset(sc, byaw)
        d.approach_banana(d.banana_pos()[:2], d.banana_axis(), box_xy=d.box_pos()[:2]); d.settle()
        p = d.ground_pick(grasp=True)
        ok3 += p["grasped"]; n3 += 1
        row.append(f"{byaw:+4.0f} deg: {p['angle_err_deg']:4.1f} deg {'OK ' if p['grasped'] else 'MISS'}")
    print(f"  scene {sid} {sc['name']:<14} " + " | ".join(row))
print(f"  => picked {ok3}/{n3}")