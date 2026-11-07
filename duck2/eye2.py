"""eye2.py - Franky's eye for the NEW scenes (N0 - N9): the same head camera as eye.py, but it can see SEVERAL bananas, a
GREEN box and a BLUE box.

eye.py is not changed: Eye2 is the old Eye (same camera mounting, same colour rules, same table-plane geometry) with a new look().
    look() -> {"bananas": [ {ahead, left, dist, bearing_deg, pixels, cut, angle_deg}, ... nearest first ],
               "green": None or {ahead, left, ...}, "blue": None or {ahead, left, ...}}
Like before it uses ONLY the camera picture (+ the camera's own position and the known table height). It never reads where
anything really is. Two yellow patches that are less than 6 cm apart on the table count as ONE banana (a banana that a
tray wall cuts in two is still one banana).

OracleEye2 = "perfect eyes" for comparison: reads the simulator, sees every banana and box everywhere, also behind the duck.
"""

import math

import numpy as np

from sim.eye import BANANA_MIN_PX, BANANA_Z, BOX_MIN_PX, BOX_Z, H, W, Eye, colour_masks

MERGE_M = 0.06           # two yellow patches closer than this (m, on the table) are one banana
MAX_BANANAS_SEEN = 4


def colour_masks2(img):
    """yellow (banana), green (box), blue (box). Blue box = a strongly blue pixel; the blue floor and the sky are paler."""
    yellow, green = colour_masks(img)
    r, g, b = (img[..., i].astype(np.int32) for i in range(3))
    blue = (b > 120) & (b * 10 > r * 40) & (b * 10 > g * 26)
    return yellow, green, blue


def _blobs(mask, min_px):
    """[(xs, ys), ...] of every group of touching pixels with at least min_px pixels, biggest first."""
    ys, xs = np.nonzero(mask)
    pts = set(zip(xs.tolist(), ys.tolist()))
    seen, out = set(), []
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
        if len(comp) >= min_px:
            a = np.array(comp)
            out.append((a[:, 0], a[:, 1]))
    out.sort(key=lambda t: -len(t[0]))
    return out


class Eye2(Eye):
    def _box(self, mask):
        if mask.sum() < BOX_MIN_PX:
            return None
        ys, xs = np.nonzero(mask)
        pts = self._table_points(xs, ys, BOX_Z)
        if len(pts) < BOX_MIN_PX:
            return None
        rep = self._report(pts, len(pts))
        rep["cut"] = bool(ys.max() >= H - 1 or xs.min() <= 0 or xs.max() >= W - 1)
        return rep

    def _bananas(self, yellow):
        """One report per banana seen, nearest first."""
        blobs = []
        for xs, ys in _blobs(yellow, BANANA_MIN_PX)[:MAX_BANANAS_SEEN * 2]:
            pts = self._table_points(xs, ys, BANANA_Z)
            if len(pts) >= BANANA_MIN_PX:
                blobs.append([xs, ys, pts])
        merged = True
        while merged and len(blobs) > 1:                              # glue patches that belong to one banana
            merged = False
            for i in range(len(blobs)):
                for j in range(i + 1, len(blobs)):
                    if np.linalg.norm(blobs[i][2].mean(0) - blobs[j][2].mean(0)) < MERGE_M:
                        blobs[i] = [np.concatenate([blobs[i][0], blobs[j][0]]), np.concatenate([blobs[i][1], blobs[j][1]]),
                                    np.concatenate([blobs[i][2], blobs[j][2]])]
                        del blobs[j]
                        merged = True
                        break
                if merged:
                    break
        out = []
        for xs, ys, pts in blobs:
            rep = self._report(pts, len(xs))
            rep["cut"] = bool(ys.max() >= H - 1 or xs.min() <= 0 or xs.max() >= W - 1)
            rep["angle_deg"] = self._banana_angle(pts)
            out.append(rep)
        out.sort(key=lambda r: r["dist"])
        return out[:MAX_BANANAS_SEEN]

    def look(self, img=None):
        img = self.picture() if img is None else img
        yellow, green, blue = colour_masks2(img)
        return {"bananas": self._bananas(yellow) if yellow.sum() >= BANANA_MIN_PX else [],
                "green": self._box(green), "blue": self._box(blue)}


class OracleEye2:
    """'Perfect eyes', for comparison: every banana and both boxes, everywhere (also behind the duck), with no error."""

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
        out = {"bananas": [], "green": self._report(d.box_pos()[:2]),
               "blue": self._report(d.blue_pos()[:2]) if d.blue_present else None}
        for i in range(len(d.present)):
            if d.banana_loc(i) in ("table", "green", "blue"):
                rep = self._report(d.banana_xyz(i)[:2])
                a = math.degrees(d._wrap(d.banana_axis_of(i) - d.pose()[2]))
                rep["angle_deg"] = round((a + 90.0) % 180.0 - 90.0, 1)
                out["bananas"].append(rep)
        out["bananas"].sort(key=lambda r: r["dist"])
        return out