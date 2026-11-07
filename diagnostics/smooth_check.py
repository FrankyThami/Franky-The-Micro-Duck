"""smooth_check.py - where does the "not smooth" come from?  (about 30 s, window open, real time)

Four short phases: standing, walking straight, turning on the spot, and the camera taking pictures.
For each phase it prints how long the computer needs for one 20 ms step of the duck, how evenly the window gets its
pictures, and whether the simulation really runs in real time.

    python smooth_check.py                 # with the MuJoCo window (what you see in --view)
    python smooth_check.py --no-window     # same, without the window (only the physics part)
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import sys
import time

from sim.duck_sim import DT_CTRL, Duck, ViewerClosed

WINDOW = "--no-window" not in sys.argv
d = Duck("Franky", viewer=WINDOW, realtime=WINDOW)
try:
    from sim.eye import Eye
    d.eye = Eye(d)
except Exception as e:                                     # the check still works without the camera
    d.eye = None
    print(f"(the camera could not be started: {e})")


def reset_counters():
    d.stats = dict(ticks=0, work_sum=0.0, work_max=0.0, sync_sum=0.0, sync_max=0.0)
    v = d.viewer
    if v is not None and d._fast:
        v.n = v.late = v.pauses = 0
        v.dt_sum = v.dt_max = v.draw_sum = v.draw_max = 0.0


def phase(title, seconds, vx=0.0, wz=0.0):
    reset_counters()
    print(f"\n--- {title} ({seconds:.0f} s of simulation)", flush=True)
    t0, s0 = time.perf_counter(), d.t
    if vx == 0.0 and wz == 0.0:
        d.hold(seconds)
    else:
        d.walk(vx, 0.0, wz, seconds)
    wall, sim = time.perf_counter() - t0, d.t - s0
    print(f"  wall clock {wall:.2f} s for {sim:.2f} s of simulation  (speed x{sim / wall:.2f}; x1.00 = real time)")
    for line in d.timing_report().splitlines():
        print("  " + line)


try:
    d.hold(1.0)
    phase("1. standing still", 3.0)
    phase("2. walking straight", 8.0, vx=0.4)
    d.hold(0.5)
    phase("3. turning on the spot", 4.0, wz=1.0)
    d.hold(0.5)
    if d.eye is not None:
        print("\n--- 4. the camera takes 6 pictures (the window cannot move while one is being taken)", flush=True)
        ms = []
        for _ in range(6):
            t0 = time.perf_counter()
            d.eye.look()
            ms.append(1000 * (time.perf_counter() - t0))
            d.hold(0.5)
        print("  one picture took: " + ", ".join(f"{m:.0f}" for m in ms) + f" ms   (average {sum(ms) / len(ms):.0f} ms, worst {max(ms):.0f} ms)")
    print("\nDone.")
except ViewerClosed:
    print("\nwindow closed - stopped.")
finally:
    if d.eye is not None:
        d.eye.close()
    d.close()