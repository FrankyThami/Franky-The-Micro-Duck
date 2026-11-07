"""eye.py - Franky's eye (Step 2, v3): the corrected head camera + a simple colour-based "what do I see?".

The camera that comes with the robot model ("head_camera") is mounted facing BACKWARDS and turned on its
side, so it only sees the inside of the duck's head (the green circuit board). fix_eye() re-mounts it.
It is done in memory only: no model file and no other file of yours is changed.

The new mounting (the same for every duck, so the model stays the only difference):
    - looks straight ahead along the beak, tilted DOWN by TILT_DEG so the table in front of the beak is in view
    - wide-angle lens, HFOV_DEG degrees wide

Eye.look() uses ONLY the camera picture (plus the camera's own position, which a real robot gets from its
joint angles and odometry, and the known height of the table top). It never reads where the banana or the box
really are. What it returns is in the duck's own frame, the same numbers the duck's tools use:
    ahead = metres in front of the trunk, left = metres to the left, bearing = degrees (+ = left),
    angle = direction of the banana's long side RELATIVE TO THE DUCK'S BEAK, in degrees (0 = lying along the
            beak, +-90 = lying across it).
    cut   = True when the object touches the edge of the picture (so only a part of it is seen and its
            middle is less certain).

v3 adds: the `cut` flag, and OracleEye ("perfect eyes", reads the simulator) for comparing the camera with
an all-knowing duck.
"""

import math

import mujoco
import numpy as np

from sim.duck_sim import TABLE_H

CAM = "head_camera"
TILT_DEG = 35.0        # how far the camera looks down from the horizontal (the head itself adds ~10 deg more)
HFOV_DEG = 100.0       # horizontal field of view of the lens
W, H = 640, 480        # picture size in pixels

BANANA_MIN_PX = 8      # fewer yellow pixels than this = no banana seen
BOX_MIN_PX = 30        # fewer green pixels than this = no box seen
BANANA_Z = 0.011       # the banana's middle is ~1.1 cm above the table top (prior knowledge)
BANANA_DIA = 0.018     # the banana is ~1.8 cm thick (prior knowledge)
BOX_Z = 0.008          # the tray's floor + walls, on average


def _qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def fix_eye(model, tilt_deg=TILT_DEG, hfov_deg=HFOV_DEG):
    """Re-mount the head camera (in memory). Call it once, right after the Duck is built."""
    cid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, CAM)
    if cid < 0:
        raise RuntimeError(f"The model has no camera called '{CAM}'.")
    level = np.array([math.cos(-math.pi / 4), 0.0, 0.0, math.sin(-math.pi / 4)])   # straight ahead, picture-up = head-up
    a = math.radians(-tilt_deg) / 2
    model.cam_quat[cid] = _qmul(level, np.array([math.cos(a), math.sin(a), 0.0, 0.0]))   # then tilt down
    model.cam_fovy[cid] = math.degrees(2 * math.atan(math.tan(math.radians(hfov_deg) / 2) * H / W))
    return cid


def colour_masks(img):
    """banana = yellow (red ~ green, little blue), box = green. The brown table and the blue floor do not match."""
    r, g, b = (img[..., i].astype(np.int32) for i in range(3))
    yellow = (r > 80) & (b * 10 < r * 3) & (g * 100 > r * 78)
    green = (g > 60) & (g * 10 > r * 14) & (g * 10 > b * 13)
    return yellow, green


def _largest_blob(mask):
    """(xs, ys) of the biggest group of touching pixels (ignores stray single pixels)."""
    ys, xs = np.nonzero(mask)
    pts = set(zip(xs.tolist(), ys.tolist()))
    seen, best = set(), []
    for p in pts:
        if p in seen:
            continue
        seen.add(p)
        stack, comp = [p], []
        while stack:
            x, y = stack.pop()
            comp.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in pts and q not in seen:
                        seen.add(q)
                        stack.append(q)
        if len(comp) > len(best):
            best = comp
    if not best:
        return np.array([], int), np.array([], int)
    a = np.array(best)
    return a[:, 0], a[:, 1]


class Eye:
    def __init__(self, duck):
        self.duck = duck
        self.cid = fix_eye(duck.model)
        self.cam = mujoco.Renderer(duck.model, height=H, width=W)
        self._give_window_back()

    def close(self):
        self.cam.close()

    def _give_window_back(self):
        """If the MuJoCo window is open, give it its drawing context back (the camera used another one)."""
        v = getattr(self.duck, "viewer", None)
        if v is not None and getattr(self.duck, "_fast", False):
            v.glfw.make_context_current(v.win)

    def picture(self):
        """One picture from the head camera (H x W x 3, uint8)."""
        d = self.duck
        mujoco.mj_forward(d.model, d.data)
        self.cam.update_scene(d.data, camera=CAM)
        img = self.cam.render().copy()
        self._give_window_back()
        return img

    def _table_points(self, xs, ys, plane_z):
        """Where do these picture pixels touch the plane `plane_z` m above the table? -> N x 2 world (x, y)."""
        d = self.duck
        pos, R = d.data.cam_xpos[self.cid], d.data.cam_xmat[self.cid].reshape(3, 3)
        f = (H / 2) / math.tan(math.radians(d.model.cam_fovy[self.cid]) / 2)
        rays = np.stack([(xs - W / 2) / f, -(ys - H / 2) / f, -np.ones(len(xs))], axis=1) @ R.T
        ok = rays[:, 2] < -1e-6
        t = (TABLE_H + plane_z - pos[2]) / rays[ok, 2]
        return (pos + t[:, None] * rays[ok])[:, :2]

    def _report(self, pts, n_px):
        c = pts.mean(axis=0)
        r = self.duck.reach(c)
        return {"ahead": r["ahead"], "left": r["left"], "dist": round(math.hypot(r["ahead"], r["left"]), 3),
                "bearing_deg": round(math.degrees(math.atan2(r["left"], r["ahead"])), 1), "pixels": int(n_px)}

    def _banana_angle(self, pts):
        """Direction of the banana's long side relative to the beak, in degrees (0 = along the beak, +-90 = across).
        The banana is thick, so from a low camera angle its picture is smeared along the line of sight; the
        smear (thickness / tan(viewing angle)) is taken out of the blob's spread before its long side is measured."""
        d = self.duck
        c = pts.mean(axis=0)
        cam = d.data.cam_xpos[self.cid]
        u = c - cam[:2]
        dist = float(np.linalg.norm(u)) or 1e-6
        u = u / dist
        elev = math.atan2(cam[2] - (TABLE_H + BANANA_Z), dist)
        smear = BANANA_DIA / math.tan(max(elev, 0.05))
        cov = np.cov((pts - c).T) - (smear ** 2 / 12.0) * np.outer(u, u)
        w, V = np.linalg.eigh(cov)
        w = np.maximum(w, 1e-9)
        if math.sqrt(w[1] / w[0]) < 1.5:                       # blob is round: seen end-on, angle unknown
            return None
        a = math.atan2(V[1, 1], V[0, 1]) - d.pose()[2]
        return round(math.degrees((a + math.pi / 2) % math.pi - math.pi / 2), 1)

    def look(self, img=None):
        """What does the duck see right now? -> {"banana": None or dict, "box": None or dict}."""
        img = self.picture() if img is None else img
        yellow, green = colour_masks(img)
        out = {"banana": None, "box": None}
        if yellow.sum() >= BANANA_MIN_PX:
            xs, ys = _largest_blob(yellow)
            if len(xs) >= BANANA_MIN_PX:
                pts = self._table_points(xs, ys, BANANA_Z)
                rep = self._report(pts, len(xs))
                rep["cut"] = bool(ys.max() >= H - 1 or xs.min() <= 0 or xs.max() >= W - 1)
                rep["angle_deg"] = None
                if len(pts) >= BANANA_MIN_PX:
                    rep["angle_deg"] = self._banana_angle(pts)
                out["banana"] = rep
        if green.sum() >= BOX_MIN_PX:
            ys, xs = np.nonzero(green)
            pts = self._table_points(xs, ys, BOX_Z)
            if len(pts) >= BOX_MIN_PX:
                out["box"] = self._report(pts, len(pts))
                out["box"]["cut"] = bool(ys.max() >= H - 1 or xs.min() <= 0 or xs.max() >= W - 1)
        return out


class OracleEye:
    """'Perfect eyes', for comparison: sees the banana and the box everywhere (also behind the duck), with no
    error, because it reads the simulator. Same answers as Eye.look(), so the tools do not care which one they get."""

    def __init__(self, duck):
        self.duck = duck

    def close(self):
        pass

    def _report(self, xy):
        r = self.duck.reach(xy)
        return {"ahead": r["ahead"], "left": r["left"], "dist": round(math.hypot(r["ahead"], r["left"]), 3),
                "bearing_deg": round(math.degrees(math.atan2(r["left"], r["ahead"])), 1), "pixels": 9999, "cut": False}

    def look(self, img=None):
        d = self.duck
        out = {"banana": None, "box": self._report(d.box_pos()[:2])}
        if d.banana_pos()[2] > TABLE_H - 0.05:                  # (a banana that fell off the table is gone)
            out["banana"] = self._report(d.banana_pos()[:2])
            a = math.degrees(d._wrap(d.banana_axis() - d.pose()[2]))
            out["banana"]["angle_deg"] = round((a + 90.0) % 180.0 - 90.0, 1)
        return out