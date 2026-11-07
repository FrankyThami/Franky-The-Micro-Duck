"""duck2.py - Franky's body for the NEW scenes (N0 - N9): the old Duck + a BLUE box + a SECOND banana.

duck_sim.py is not changed. Duck2 is the same duck (same walking, same crouch, same simulated grasp) in a world that
has up to two bananas and up to two boxes (a green one and a blue one). Anything that is not in a scene is hidden:
invisible, no collisions, parked under the floor, so the camera can never see it.

How the old one-banana code keeps working: the duck always has one "active" banana (select(i)). The old skills
(ground_pick, approach_banana, ... all read self.ban_q / self.ban_body / ...) simply work on the active one.
ground_pick(grasp=True) first makes the banana nearest to the beak the active one, like a real beak would grab the
banana that is right under it.

New in this file:
    reset(seed, duck_xy, duck_yaw, bananas=[{xy, yaw}, ...], green_xy, blue_xy)    (a scene)
    banana_loc(i)      "table" / "green" / "blue" / "held" / "fell" / "absent"        (the referee's ground truth)
    walk_to_box()      walk to a box and keep clear of the other box and of bananas on the way (long straight last leg)
    keepout            things approach_banana() must keep clear of (set by the tools just before it runs)
"""

import math

import mujoco
import numpy as np

from sim.duck_sim import (BANANA_CLEAR, BOX_CLEAR, DEFAULT_POSE, HOLD_OFFSET, PAD_HALF, STANDOFF, TABLE_H, Duck)

BLUE_RGBA = [0.08, 0.30, 0.95, 1.0]     # saturated blue: the dark blue-grey floor and the sky are far less saturated
PARK_Z = -2.0                           # hidden things wait this far below the floor
BEAK_AHEAD = 0.105                      # the beak tip is this far in front of the trunk when the duck crouches
MAX_BANANAS = 2


class Duck2(Duck):
    # ------------------------------------------------------------------ model
    def _add_world(self, spec):
        super()._add_world(spec)                                      # table, banana, green box (as before)
        G = mujoco.mjtGeom
        wb = spec.worldbody
        b = wb.add_body(name="banana2", pos=[0.2, 0.3, TABLE_H + 0.01])
        b.add_freejoint(name="banana2_free")
        b.add_geom(name="banana2_geom", type=G.mjGEOM_CAPSULE, size=[0.009, 0.028, 0], quat=[0.7071, 0, 0.7071, 0],
                   mass=0.01, rgba=[1.0, 0.85, 0.1, 1], friction=[0.8, 0.005, 0.0001])
        bb = wb.add_body(name="blue_box", pos=[0.2, -0.3, TABLE_H])
        bb.add_geom(name="blue_floor", type=G.mjGEOM_BOX, size=[PAD_HALF + 0.012, PAD_HALF + 0.012, 0.003],
                    pos=[0, 0, 0.003], rgba=BLUE_RGBA)
        for nm_, p, s in (("bw_n", [0, PAD_HALF + 0.006, 0.008], [PAD_HALF + 0.012, 0.006, 0.008]),
                          ("bw_s", [0, -PAD_HALF - 0.006, 0.008], [PAD_HALF + 0.012, 0.006, 0.008]),
                          ("bw_e", [PAD_HALF + 0.006, 0, 0.008], [0.006, PAD_HALF + 0.012, 0.008]),
                          ("bw_w", [-PAD_HALF - 0.006, 0, 0.008], [0.006, PAD_HALF + 0.012, 0.008])):
            bb.add_geom(name=nm_, type=G.mjGEOM_BOX, size=s, pos=p, rgba=BLUE_RGBA)

    def _build_model(self):
        super()._build_model()
        m = self.model
        nm = lambda t, n: mujoco.mj_name2id(m, t, n)
        O = mujoco.mjtObj
        j2 = nm(O.mjOBJ_JOINT, "banana2_free")
        self._B = [dict(q=self.ban_q, v=self.ban_v, body=self.ban_body, geom=self.ban_geom),
                   dict(q=int(m.jnt_qposadr[j2]), v=int(m.jnt_dofadr[j2]), body=nm(O.mjOBJ_BODY, "banana2"),
                        geom=nm(O.mjOBJ_GEOM, "banana2_geom"))]
        self.blue_body = nm(O.mjOBJ_BODY, "blue_box")
        self.box_geoms = {"green": [g for g in range(m.ngeom) if m.geom_bodyid[g] == self.box_body],
                          "blue": [g for g in range(m.ngeom) if m.geom_bodyid[g] == self.blue_body]}
        every = [B["geom"] for B in self._B] + self.box_geoms["green"] + self.box_geoms["blue"]
        self._look0 = {g: (m.geom_rgba[g].copy(), int(m.geom_contype[g]), int(m.geom_conaffinity[g])) for g in every}
        self.present = [True, False]                                  # which bananas exist in the current scene
        self.green_present, self.blue_present = True, False
        self.active = 0
        self.keepout = []                                             # [(xy, clearance)]: approach_banana keeps clear of these

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._quat_adr.append(self._B[1]["q"] + 3)                    # smooth drawing also for the second banana's rotation

    # ------------------------------------------------------------------ what exists
    def _show(self, geoms, on):
        m = self.model
        for g in geoms:
            rgba, ct, ca = self._look0[g]
            m.geom_rgba[g] = rgba if on else rgba * np.array([1, 1, 1, 0.0])
            m.geom_contype[g] = ct if on else 0
            m.geom_conaffinity[g] = ca if on else 0

    def select(self, i):
        """Make banana i the one the old one-banana skills work on (not while a banana is in the beak)."""
        if self.holding:
            return
        B = self._B[i]
        self.active = i
        self.ban_q, self.ban_v, self.ban_body, self.ban_geom = B["q"], B["v"], B["body"], B["geom"]

    # ------------------------------------------------------------------ reset = one scene
    def reset(self, seed=0, duck_xy=(-0.40, 0.0), duck_yaw=0.0, bananas=None, green_xy=None, blue_xy=None, **_ignored):
        """bananas = [{"xy": (x, y), "yaw": rad}, ...] (0, 1 or 2 of them); blue_xy = None -> no blue box.
        Without bananas / green_xy: the same random layout the old Duck makes from the seed (one banana, no blue box)."""
        rng = np.random.default_rng(seed)
        side = rng.choice([-1.0, 1.0])
        if bananas is None:
            bananas = [dict(xy=(rng.uniform(0.10, 0.25), side * rng.uniform(0.08, 0.20)), yaw=0.0)]
        if green_xy is None:
            green_xy = (rng.uniform(0.10, 0.25), -side * rng.uniform(0.15, 0.25))
        m, d = self.model, self.data
        mujoco.mj_resetData(m, d)
        q = self.free_q
        d.qpos[q:q + 3] = [duck_xy[0], duck_xy[1], TABLE_H + 0.125]
        d.qpos[q + 3:q + 7] = [math.cos(duck_yaw / 2), 0, 0, math.sin(duck_yaw / 2)]
        for i, qi in enumerate(self.q_idx):
            d.qpos[qi] = DEFAULT_POSE[i]
        m.body_pos[self.box_body] = [green_xy[0], green_xy[1], TABLE_H]
        self.blue_present = blue_xy is not None
        m.body_pos[self.blue_body] = [blue_xy[0], blue_xy[1], TABLE_H] if self.blue_present else [0.0, 0.0, PARK_Z]
        self._show(self.box_geoms["green"], True)
        self._show(self.box_geoms["blue"], self.blue_present)
        boxes = [green_xy] + ([blue_xy] if self.blue_present else [])
        self.present = [i < len(bananas) for i in range(MAX_BANANAS)]
        for i, B in enumerate(self._B):
            self._show([B["geom"]], self.present[i])
            m.body_gravcomp[B["body"]] = 0.0 if self.present[i] else 1.0
            if self.present[i]:
                (x, y), yaw = bananas[i]["xy"], bananas[i].get("yaw", 0.0)
                in_box = any(abs(x - bx) < PAD_HALF and abs(y - by) < PAD_HALF for bx, by in boxes)
                d.qpos[B["q"]:B["q"] + 3] = [x, y, TABLE_H + (0.017 if in_box else 0.011)]
                d.qpos[B["q"] + 3:B["q"] + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
            else:
                d.qpos[B["q"]:B["q"] + 3] = [0.0, 0.0, PARK_Z]
                d.qpos[B["q"] + 3:B["q"] + 7] = [1, 0, 0, 0]
        self.holding = False
        self.keepout = []
        self.select(0)
        self.bam.reset(d.qpos)
        self.bam.q_target[:] = DEFAULT_POSE
        mujoco.mj_forward(m, d)
        self._hn = 0                      # teleport: forget the old states so nothing is blended across it
        self._hist_push()
        self.t = 0.0
        self.last_action = np.zeros(14, dtype=np.float32)
        self.twist = np.zeros(3, dtype=np.float32)
        self.pick_phase = None
        self.sess = self.walk_sess
        self.seed, self.box_xy = seed, tuple(green_xy)
        self.banana_start = tuple(bananas[0]["xy"]) if bananas else None
        self.tick(25)                     # let it settle (standing, no command)
        self.t = 0.0

    # ------------------------------------------------------------------ truth (for the referee and the oracle, never shown to a duck)
    def banana_xyz(self, i):
        return self.data.xpos[self._B[i]["body"]].copy()

    def banana_axis_of(self, i):
        ax = self.data.xmat[self._B[i]["body"]].reshape(3, 3)[:, 0]
        return math.atan2(float(ax[1]), float(ax[0]))

    def grasp_err_of(self, i):
        a = abs(math.degrees(self._wrap(self.banana_axis_of(i) - self.pose()[2] - HOLD_OFFSET)))
        return min(a, 180.0 - a)

    def blue_pos(self):
        return self.data.xpos[self.blue_body].copy()

    def box_xyz(self, key):
        return self.box_pos() if key == "green" else self.blue_pos()

    def banana_loc(self, i):
        """'absent' | 'held' | 'fell' | 'green' | 'blue' | 'table' for banana i."""
        if not self.present[i]:
            return "absent"
        if self.holding and self.active == i:
            return "held"
        p = self.banana_xyz(i)
        if p[2] < TABLE_H - 0.05:
            return "fell"
        if p[2] < TABLE_H + 0.03:
            for key, ok in (("green", True), ("blue", self.blue_present)):
                c = self.box_xyz(key)
                if ok and abs(p[0] - c[0]) < PAD_HALF and abs(p[1] - c[1]) < PAD_HALF:
                    return key
        return "table"

    # ------------------------------------------------------------------ skills (the old ones, made aware of two bananas / two boxes)
    def ground_pick(self, grasp=False, release=False):
        if grasp and not self.holding:                                # the banana under the beak is the one that gets grabbed
            x, y, yaw, _ = self.pose()
            beak = (x + BEAK_AHEAD * math.cos(yaw), y + BEAK_AHEAD * math.sin(yaw))
            near = [(math.hypot(self.banana_xyz(i)[0] - beak[0], self.banana_xyz(i)[1] - beak[1]), i)
                    for i in range(MAX_BANANAS) if self.present[i]]
            if near:
                self.select(min(near)[1])
        return super().ground_pick(grasp, release)

    def _plan_approach(self, c, axis, box_xy):
        """Same plan as duck_sim.Duck._plan_approach, but it keeps clear of every box and every other banana it was told
        about (self.keepout), not only of one box."""
        here = np.array(self.pose()[:2])
        clear = ([(np.asarray(box_xy, float), BOX_CLEAR)] if box_xy is not None else []) + \
                [(np.asarray(xy, float), r) for xy, r in self.keepout]
        avoid = [(c, BANANA_CLEAR)] + clear
        best = None
        for sgn in (1.0, -1.0):
            u = sgn * np.array([math.cos(axis), math.sin(axis)])
            L = 0.35
            while L > 0.22 and not self._on_table(c - L * u):
                L -= 0.02
            Q = c - L * u
            S = c - STANDOFF * u
            cost = 3.0 * (0.35 - L)
            if not self._on_table(Q):
                cost += 10.0
            for o, r in clear:
                cost += 20.0 * max(0.0, r - self._seg_dist(o, Q, S))      # final leg would cross a box / another banana
            way = self._route(here, Q, avoid)
            pts = [here] + way
            cost += sum(float(np.linalg.norm(pts[i + 1] - pts[i])) for i in range(len(pts) - 1))
            if best is None or cost < best[0]:
                best = (cost, u, Q, way)
        return best[1], best[2], best[3]

    def _plan_box_approach(self, centre, avoid, length=0.30):
        """Choose from which side to walk the LAST straight leg into the box at `centre`, and a staging point Q `length` m
        before it. The duck comes at a SIDE of the tray, facing it (a diagonal approach puts its feet on the tray corner and
        it stumbles). Cheapest = short walk, small turn at Q, the way there and the last leg keep clear of the circles in
        `avoid` = [(xy, radius)] (the other box, bananas on the table).  -> (angle of the last leg, [waypoints ending at Q],
        the spot the beak must end up on)."""
        here = np.array(self.pose()[:2])
        centre = np.asarray(centre, float)
        circles = [(np.asarray(xy, float), r) for xy, r in avoid]
        best = None
        for base in (0.0, 90.0, 180.0, 270.0):
            for off in (-12.0, 0.0, 12.0):
                ang = math.radians(base + off)
                u = np.array([math.cos(ang), math.sin(ang)])
                target = centre - 0.035 * u                                    # the beak lands 3.5 cm before the tray centre
                L = length
                while L > 0.20 and not self._on_table(target - L * u, 0.06):
                    L -= 0.02
                Q = target - L * u
                if not self._on_table(Q, 0.06):
                    continue
                S = target - STANDOFF * u
                cost = 1.0 * abs(off) / 12.0 * 0.02
                for o, r in circles:
                    cost += 20.0 * max(0.0, r - self._seg_dist(o, Q, S)) + 20.0 * max(0.0, r - float(np.linalg.norm(o - Q)))
                way = self._route(here, Q, circles)
                pts = [here] + way
                cost += sum(float(np.linalg.norm(pts[i + 1] - pts[i])) for i in range(len(pts) - 1))
                d_in = pts[-1] - pts[-2]
                arrive = math.atan2(d_in[1], d_in[0]) if float(np.linalg.norm(d_in)) > 0.02 else self.pose()[2]
                cost += 0.15 * abs(self._wrap(arrive - ang))                   # turning at Q costs time and balance
                if best is None or cost < best[0]:
                    best = (cost, ang, way, (float(target[0]), float(target[1])))
        if best is None:                                                         # no staging point on the table: go straight
            d = centre - here
            return math.atan2(d[1], d[0]), [], (float(centre[0]), float(centre[1]))
        return best[1], best[2], best[3]

    def walk_to_box(self, centre_xy, avoid=(), max_s=45.0):
        """Walk to the box at `centre_xy` until the beak is over its middle (like walk_to()), but on the way keep clear of the
        circles in `avoid` = [(xy, radius)] (a blue box, a banana ...), come at a SIDE of the tray and finish with a LONG
        STRAIGHT leg: a short last leg after a turn, or a diagonal one, ends in a stumble next to the tray rim."""
        ang, way, target = self._plan_box_approach(centre_xy, avoid)
        for i, w in enumerate(way):                                            # ... the way to the staging point (last one)
            self.walk_to((float(w[0]), float(w[1])), standoff=0.0, tol=0.06 if i < len(way) - 1 else 0.04, max_s=max_s)
            if self.is_fallen() or self.off_table():
                return {"ok": False, "reason": "fell" if self.is_fallen() else "off_table"}
        self.settle()
        if abs(self._wrap(ang - self.pose()[2])) > math.radians(25):
            self.turn_to(ang, tol_deg=10.0)                                    # face the way of the last leg
            self.settle()
        return self.walk_to(target, max_s=max_s)