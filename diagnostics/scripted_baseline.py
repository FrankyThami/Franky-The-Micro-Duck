"""scripted_baseline.py - the duck does the whole task with NO LLM (the "oracle" baseline).

It knows the true banana / box positions and just runs the skills in order:
    walk_to(banana) -> ground_pick(grasp) -> walk_to(box) -> ground_pick(release) -> check

This is the upper bound the LLM agent will be compared against later.

    python scripted_baseline.py                 # 10 episodes, no window, prints a table
    python scripted_baseline.py --view          # watch episode 0 in the MuJoCo window
    python scripted_baseline.py --seeds 0 1 2   # chosen seeds
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import time

import math

from sim.duck_sim import Duck, ViewerClosed


def run_episode(duck: Duck, seed: int):
    duck.reset(seed=seed)
    t0 = duck.t
    log = []
    r = duck.walk_to(duck.banana_pos()[:2]); duck.settle();  log.append(("walk_to banana", r))
    if not r["ok"]:
        return False, duck.t - t0, log
    p = duck.ground_pick(grasp=True);                        log.append(("pick", p))
    if not p["grasped"]:
        return False, duck.t - t0, log
    # aim the beak at the NEAR part of the tray (3.5 cm before its centre) so the feet stay off the tray
    bx, by = duck.box_pos()[:2]; x, y = duck.pose()[:2]
    n = math.hypot(bx - x, by - y) or 1.0
    r = duck.walk_to((bx - 0.035 * (bx - x) / n, by - 0.035 * (by - y) / n)); duck.settle();  log.append(("walk_to box", r))
    if not r["ok"]:
        return False, duck.t - t0, log
    p = duck.ground_pick(release=True);                      log.append(("place", p))
    duck.hold(1.5)                                           # let the banana settle
    return duck.banana_in_box(), duck.t - t0, log


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="*", default=list(range(10)))
    ap.add_argument("--view", action="store_true", help="open the MuJoCo window (real-time)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0, help="1.0 real time, 0.5 slow motion (with --view)")
    args = ap.parse_args()

    duck = Duck("Franky", viewer=args.view, realtime=args.view, speed=args.speed)
    wins, times = 0, []
    print(f"{'seed':>4} {'success':>8} {'sim_s':>7}  note")
    try:
        for s in args.seeds:
            w0 = time.time()
            ok, secs, log = run_episode(duck, s)
            note = "" if ok else next((f"failed at: {n} {r}" for n, r in log if not r.get('ok', r.get('grasped', True))), "banana not in box")
            print(f"{s:>4} {str(ok):>8} {secs:7.1f}  {note}   (wall {time.time()-w0:.0f}s)")
            if args.verbose:
                for n, r in log:
                    print("      ", n, r)
            wins += ok
            times.append(secs)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    if times:
        print(f"\nsuccess {wins}/{len(times)}   mean sim time {sum(times)/len(times):.1f}s")
    if args.view:
        print(duck.timing_report())
    duck.close()
