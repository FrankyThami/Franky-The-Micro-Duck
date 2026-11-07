"""motion.py - Franky's body as ONE low-level command: drive(forward, turn).

The agent's old tool was walk_to(banana): the code planned the whole path, chose which side to come from and where to stop.
Now the LLM decides how far to go and when to turn. This file is only the body's motor skill, nothing more:

    1. turn on the spot by `turn` degrees (+ = left)  - or, when the turn is small, blend it into the walk (see below)
    2. walk straight `forward` metres (negative = backwards), holding the heading with the duck's own odometry
    3. stand still until the duck stops drifting, then report what the body REALLY did (odometry): turned X deg, moved Y m

It does not know where the banana or the box are, and it does not aim at anything. The real movement is a little different
from the command (a few degrees, a few centimetres) - that is the body, and the agent sees the real numbers.

MEASURED on this body (velstand.onnx on the table):
  * turning on the spot by 45 deg or more is accurate (+-3 deg with tol 4); turning by less than ~30 deg on the spot springs back
    (hips twist, the feet do not step) -> a small turn is made GENTLY WHILE WALKING (heading hold), which is accurate to ~5 deg
  * straight walking is accurate to ~1 cm per 30 cm; backwards the duck coasts ~3 cm further, which is compensated

SAFETY LAYER (always on, like a real robot's cliff sensor and bumper - the LLM cannot talk it away). It uses the TRUE positions:
  * table edge : a walk whose end point would be closer than EDGE_MARGIN to the edge is refused; a walk that gets too close is stopped
  * box rim    : the tray rim is a trip hazard: a walk that would put the body centre closer than BOX_KEEPOUT to the box centre
                 (or run through the box) is refused; one that gets there is stopped
  * bumper     : the walk stops when the banana is right at the feet (it would be kicked off the table otherwise)
"""

import math

import numpy as np

from sim.duck_sim import TABLE_H, TABLE_HALF, Duck

# ---- limits of the command
MAX_FORWARD = 0.60            # m
MAX_BACK = 0.25               # m
MAX_TURN = 180.0              # deg
TURN_IN_PLACE_MIN = 30.0      # deg: a turn this big is made on the spot; a smaller one is blended into the walk
ARC_MIN_FORWARD = 0.10        # m: ... which needs at least this much forward walk
TURN_TOL = 4.0                # deg: tolerance of the on-the-spot turn
IGNORE_TURN = 3.0             # deg: a "turn" smaller than this is not worth doing
TURN_CREEP = 0.04             # m: MEASURED - a turn on the spot shuffles the body up to 3.4 cm forward along its new heading (and up to 3 cm sideways)
# ---- walking
WALK_VX = 0.4
STEER_GAIN = 1.0              # rad/s of turn per rad of heading error while walking
STEER_MAX = 0.7
STEER_BIAS_FWD = 0.0          # rad/s. MEASURED: after a forward walk this body ends ~7 deg to the LEFT of where it was told to face (the
                              # tool reports it as "turned 7 degrees to the left"), but its PATH is straight. A negative bias (-0.12) cancels the
                              # final heading but bends the path ~5 cm to the right per 0.5 m, which the odometry does not show; solving scenes got
                              # worse with it (0/3 instead of 2/3), so it is left at 0: errors stay visible in the numbers the agent is given.
LEAD_FORWARD = 0.008          # m: stop this early (the duck coasts ~1 cm when told to stop)
LEAD_BACK = 0.025             # m: backwards it coasts ~3 cm
# ---- safety (always on)
EDGE_MARGIN = 0.09            # m: the duck's body centre must stay this far from the table edge (it drifts ~4 cm when it stops)
EDGE_STOP = 0.07              # m: while walking, stop here if still heading outwards
BOX_KEEPOUT = 0.125           # m: body centre to box centre (the feet reach ~6 cm ahead of the body centre; the tray is 8 cm to its rim)
BUMP_AHEAD = 0.085            # m: the banana this close ahead of the body centre (and in front of the feet) stops the walk
BUMP_SIDE = 0.05              # m


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _seg_dist(p, a, b):
    return Duck._seg_dist(p, a, b)


def _banana_on_table(d):
    return (not d.holding) and d.banana_pos()[2] > TABLE_H - 0.05


# =============================================================================== the safety layer
def refusal(d, forward, turn_deg):
    """Why this command must be refused BEFORE the body moves, or None. Uses the duck's own pose and the true table / box."""
    x, y, yaw, _ = d.pose()
    turning = abs(turn_deg) >= IGNORE_TURN
    h = yaw + math.radians(turn_deg if turning else 0.0)
    walking = abs(forward) > 0.01
    reach = forward if walking else (TURN_CREEP if turning else 0.0)       # a turn on the spot also shuffles the body forward a little
    ex, ey = x + reach * math.cos(h), y + reach * math.sin(h)             # (the shuffle counts for the table edge ...)
    wx, wy = x + forward * math.cos(h), y + forward * math.sin(h)           # ... but not for the box rim
    # table edge: the end point must be inside the margin - unless the duck is already outside it and this brings it back
    for v0, v1, half in ((x, ex, TABLE_HALF[0]), (y, ey, TABLE_HALF[1])):
        lim = half - EDGE_MARGIN
        if abs(v1) > lim and abs(v1) > abs(v0) + 1e-6:
            return ("that walk would take you too close to the edge of the table, where you could fall off" if walking else
                    "that turn would shuffle you towards the edge of the table, where you could fall off")
    # box rim
    c = d.box_pos()[:2]
    a, b = np.array([x, y]), np.array([wx, wy])
    if float(np.linalg.norm(a - c)) < BOX_KEEPOUT:
        if float(np.linalg.norm(b - c)) < float(np.linalg.norm(a - c)) - 0.005:
            return "that walk would put your feet on the rim of the box, which would trip you"
    elif _seg_dist(c, a, b) < BOX_KEEPOUT:
        return "that walk would run you into the box (its rim would trip you)"
    return None


def _stop_reason(d, sgn, u):
    """Is the duck in front of something it must stop for, right now? -> text or None."""
    x, y, yaw, _ = d.pose()
    vx, vy = sgn * u[0], sgn * u[1]                                  # the direction the body is travelling
    if (abs(x) > TABLE_HALF[0] - EDGE_STOP and x * vx > 0) or (abs(y) > TABLE_HALF[1] - EDGE_STOP and y * vy > 0):
        return "the table edge is right there"                      # (only when heading outwards)
    c = d.box_pos()[:2]
    dist_now = math.hypot(x - c[0], y - c[1])
    if dist_now < BOX_KEEPOUT - 0.004 and math.hypot(x + 0.02 * vx - c[0], y + 0.02 * vy - c[1]) < dist_now:
        return "the rim of the box is right at your feet"             # (only when heading closer)
    if sgn > 0 and _banana_on_table(d):
        b = d.banana_pos()
        r = d.reach((float(b[0]), float(b[1])))
        if 0.0 < r["ahead"] < BUMP_AHEAD and abs(r["left"]) < BUMP_SIDE:
            return "something (the banana) is right at your feet"
    return None


# =============================================================================== the motion command
def drive(d, forward=0.0, turn_deg=0.0):
    """Turn, then walk straight. -> dict(turned_deg, moved_m, seconds, stopped, fell, off_table). The caller does the refusal check."""
    t0 = d.t
    x0, y0, yaw0, _ = d.pose()
    turn = turn_deg if abs(turn_deg) >= IGNORE_TURN else 0.0
    target = yaw0 + math.radians(turn)
    u = (math.cos(target), math.sin(target))
    walk = abs(forward) > 0.01
    stopped = None

    blend = bool(turn) and abs(turn) < TURN_IN_PLACE_MIN and walk and forward >= ARC_MIN_FORWARD
    if turn and not blend:                                         # turn on the spot (also a small one if there is no room to walk)
        d.turn_to(target, tol_deg=TURN_TOL, max_s=10.0)
        d.settle()
        if not d.is_fallen() and abs(math.degrees(_wrap(target - d.pose()[2]))) > 12.0 and abs(turn) >= TURN_IN_PLACE_MIN:
            d.turn_to(target, tol_deg=TURN_TOL, max_s=8.0)         # one more try if the turn fell short
            d.settle()

    if walk and not d.is_fallen() and not d.off_table():
        sgn = 1.0 if forward > 0 else -1.0
        goal = abs(forward) - (LEAD_FORWARD if sgn > 0 else LEAD_BACK)
        sx, sy, _, _ = d.pose()
        t1 = d.t
        max_s = 4.0 + abs(forward) / 0.06
        while d.t - t1 < max_s:
            if d.is_fallen() or d.off_table():
                break
            x, y, yaw, _ = d.pose()
            travelled = ((x - sx) * u[0] + (y - sy) * u[1]) * sgn
            if travelled >= goal:
                break
            stopped = _stop_reason(d, sgn, u)
            if stopped:
                break
            err = _wrap(target - yaw)
            wz = STEER_GAIN * err + (STEER_BIAS_FWD if sgn > 0 else 0.0)
            d.twist[:] = [sgn * WALK_VX, 0.0, float(np.clip(wz, -STEER_MAX, STEER_MAX))]
            d.tick(5)
        d.twist[:] = 0.0
        d.hold(0.5)
        d.settle()

    d.twist[:] = 0.0
    x1, y1, yaw1, _ = d.pose()
    return {"turned_deg": math.degrees(_wrap(yaw1 - yaw0)), "moved_m": (x1 - x0) * u[0] + (y1 - y0) * u[1],
            "seconds": d.t - t0, "stopped": stopped, "fell": bool(d.is_fallen()), "off_table": bool(d.off_table())}
