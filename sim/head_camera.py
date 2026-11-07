"""head_camera.py - Franky's camera as a RAW sensor (autonomous Franky).

This is NOT the old sim/eye.py. eye.py found yellow and green blobs with hand-written code and told the duck "banana: 0.31 m
ahead". Here nothing is found, named or judged by code:

    capture()           -> Frame     one real picture from the head camera, plus where the camera and the duck were at that moment
    frame_jpeg(frame)   -> bytes     the picture as a JPEG, ready to be sent to the vision model
    table_point(frame, x, y)         the geometry of ONE picture point: where the camera ray through that pixel touches the table
                                     (pure ray / plane geometry - the code does not know what is in the picture)

Which pixel is the banana, which is the green box, which is a blue box - that is for the vision model to decide.

The camera itself is the same one as in eye.py (mounted by sim.eye.fix_eye: looks straight ahead along the beak, tilted down
35 degrees, 100 degrees wide), only the picture is rendered bigger for the vision model.
Coordinates the model uses: x and y are on a 0 - 1000 scale across the picture (x: 0 = left edge, 1000 = right edge;
y: 0 = top edge, 1000 = bottom edge), the same 1000 x 1000 convention Gemma 4 uses for its own boxes and points.

pad_square (option): the picture is put on a square canvas with grey bars above and below, and the 0 - 1000 scale then runs over the whole
square. Added as an experiment after the first real runs showed that gemma4:e2b through Ollama places things correctly from left to right
but far too low in a 4:3 picture; a square picture shows whether the 4:3 shape is the reason (pointing_check.py tests both).
Everything the model says or sees ("view" coordinates) is converted here; the geometry always uses the real camera picture.

What the duck gets "for free" like a real robot would: the camera's position and direction (from its joint angles and its own
odometry) and the height of the table top.
"""

import io
import math

import mujoco
import numpy as np

from sim.duck_sim import TABLE_H
from sim.eye import CAM, HFOV_DEG, TILT_DEG, fix_eye

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                                           # the pictures cannot be encoded without Pillow
    Image = None

FRAME_W, FRAME_H = 800, 600        # picture size sent to the vision model (4:3, like the camera)
JPEG_QUALITY = 88
SCALE = 1000.0                     # coordinates the model gives: 0 .. 1000 across the picture
MEASURE_Z = 0.010                  # m above the table top: the height of the things on the table (banana ~1-2 cm, tray 0.6-1.6 cm)
MAX_RANGE = 3.0                    # m: a point farther than this is "not measurable"


class Frame:
    """One picture + the state of the camera and the duck when it was taken (the geometry must use THESE, not later values)."""

    def __init__(self, idx, img, cam_pos, cam_mat, duck_pose, t_sim):
        self.idx, self.img = idx, img
        self.cam_pos, self.cam_mat = cam_pos, cam_mat
        self.duck_pose = duck_pose              # (x, y, yaw) of the trunk at that moment (odometry)
        self.t_sim = t_sim


class HeadCamera:
    def __init__(self, duck, width=FRAME_W, height=FRAME_H):
        if Image is None:
            raise SystemExit("Pillow is needed to send pictures to the vision model:   pip install pillow")
        self.duck, self.W, self.H = duck, int(width), int(height)
        self.cid = fix_eye(duck.model)                           # (re-)mounts the head camera in memory, as eye.py does
        duck.model.cam_fovy[self.cid] = math.degrees(2 * math.atan(math.tan(math.radians(HFOV_DEG) / 2) * self.H / self.W))
        g = duck.model.vis.global_                               # the off-screen picture must fit into the model's buffer
        g.offwidth, g.offheight = max(g.offwidth, self.W), max(g.offheight, self.H)
        self.cam = mujoco.Renderer(duck.model, height=self.H, width=self.W)
        self._give_window_back()
        self.n = 0
        self.pad_square = False
        self.f_pix = (self.H / 2) / math.tan(math.radians(float(duck.model.cam_fovy[self.cid])) / 2)   # focal length in pixels
        self._font = None

    def close(self):
        self.cam.close()

    def _give_window_back(self):
        """If the MuJoCo window is open, give it its drawing context back (the camera used another one)."""
        v = getattr(self.duck, "viewer", None)
        if v is not None and getattr(self.duck, "_fast", False):
            v.glfw.make_context_current(v.win)

    # ------------------------------------------------------------------ taking a picture
    def capture(self):
        d = self.duck
        mujoco.mj_forward(d.model, d.data)
        self.cam.update_scene(d.data, camera=CAM)
        img = self.cam.render().copy()
        self._give_window_back()
        self.n += 1
        x, y, yaw, _ = d.pose()
        return Frame(self.n, img, d.data.cam_xpos[self.cid].copy(), d.data.cam_xmat[self.cid].reshape(3, 3).copy(),
                     (x, y, yaw), d.t)

    # ------------------------------------------------------------------ view coordinates (what the model sees) <-> picture pixels
    def _canvas(self):
        """(width, height, offset_x, offset_y) in pixels of what is sent to the model."""
        if not self.pad_square:
            return self.W, self.H, 0, 0
        s = max(self.W, self.H)
        return s, s, (s - self.W) // 2, (s - self.H) // 2

    def view_to_pixel(self, x, y):
        """model coordinates (0..1000 over what it sees) -> pixel (px, py) of the real camera picture; may lie outside it."""
        cw, ch, ox, oy = self._canvas()
        return float(x) / SCALE * cw - ox, float(y) / SCALE * ch - oy

    def pixel_to_view(self, px, py):
        cw, ch, ox, oy = self._canvas()
        return (px + ox) / cw * SCALE, (py + oy) / ch * SCALE

    def in_picture(self, x, y, margin=0.0):
        """Is the view point (x, y) inside the real camera picture (not in the grey bars), `margin` units from its edges?"""
        px, py = self.view_to_pixel(x, y)
        mx, my = margin / SCALE * self._canvas()[0], margin / SCALE * self._canvas()[1]
        return mx <= px <= self.W - mx and my <= py <= self.H - my

    # ------------------------------------------------------------------ picture -> JPEG (with optional red crosses)
    def _label_font(self):
        if self._font is None:
            for n in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
                try:
                    self._font = ImageFont.truetype(n, 22)
                    break
                except OSError:
                    pass
            if self._font is None:
                try:
                    self._font = ImageFont.load_default(22)
                except TypeError:
                    self._font = ImageFont.load_default()
        return self._font

    def frame_jpeg(self, frame, marks=None, boxes=None):
        """JPEG bytes of a frame. marks = [(x, y, label)] in 0..1000 coordinates -> a red cross with a number on each spot.
        boxes = [(x1, y1, x2, y2, label)] in 0..1000 coordinates -> a red rectangle (outline only) with a number on each."""
        im = Image.fromarray(frame.img)
        cw, ch, ox, oy = self._canvas()
        if self.pad_square:
            canvas = Image.new("RGB", (cw, ch), (70, 70, 70))
            canvas.paste(im, (ox, oy))
            im = canvas
        if marks or boxes:
            dr = ImageDraw.Draw(im)
            for (bx1, by1, bx2, by2, label) in boxes or []:
                r = (bx1 / SCALE * cw, by1 / SCALE * ch, bx2 / SCALE * cw, by2 / SCALE * ch)
                dr.rectangle(r, outline=(255, 255, 255), width=5)
                dr.rectangle(r, outline=(230, 20, 20), width=3)
                dr.text((r[0] + 4, max(r[1] - 32, 2)), str(label), font=self._label_font(), fill=(230, 20, 20), stroke_width=2, stroke_fill=(255, 255, 255))
            for (mx, my, label) in marks or []:
                px, py = mx / SCALE * cw, my / SCALE * ch
                for width, colour in ((5, (255, 255, 255)), (3, (230, 20, 20))):      # white outline under a red cross
                    for (a, b) in ((7, 20), (-20, -7)):                              # four arms, OPEN in the middle: the target stays visible
                        dr.line([(px + a, py), (px + b, py)], fill=colour, width=width)
                        dr.line([(px, py + a), (px, py + b)], fill=colour, width=width)
                dr.text((px + 14, py - 38), str(label), font=self._label_font(), fill=(230, 20, 20), stroke_width=2, stroke_fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()

    # ------------------------------------------------------------------ the geometry of one picture point
    def table_point(self, frame, x, y, plane_z=MEASURE_Z):
        """Where does the camera ray through picture point (x, y) [0..1000 scale] touch the plane `plane_z` m above the table top?
        -> dict(ahead, left, distance, bearing_deg) in the DUCK's frame at the moment of the picture (ahead = m in front of the
           trunk centre, left = m to the left, bearing + = left), plus 'world' (x, y) for the referee - or None when the ray
           does not come down onto the table (a point at or above the horizon, or farther than MAX_RANGE)."""
        px, py = self.view_to_pixel(x, y)
        if not (0.0 <= px <= self.W and 0.0 <= py <= self.H):          # in the grey bars of a padded picture: not the camera's picture
            return None
        ray = np.array([(px - self.W / 2) / self.f_pix, -(py - self.H / 2) / self.f_pix, -1.0]) @ frame.cam_mat.T
        if ray[2] > -1e-6:
            return None
        t = (TABLE_H + plane_z - frame.cam_pos[2]) / ray[2]
        if t <= 0:
            return None
        wx, wy = frame.cam_pos[0] + t * ray[0], frame.cam_pos[1] + t * ray[1]
        dx, dy = wx - frame.duck_pose[0], wy - frame.duck_pose[1]
        c, s = math.cos(-frame.duck_pose[2]), math.sin(-frame.duck_pose[2])
        ahead, left = c * dx - s * dy, s * dx + c * dy
        dist = math.hypot(ahead, left)
        if dist > MAX_RANGE:
            return None
        return {"ahead": ahead, "left": left, "distance": dist, "bearing_deg": math.degrees(math.atan2(left, ahead)),
                "world": (wx, wy)}

    def pixel_of_world(self, frame, wx, wy, wz):
        """Inverse of table_point, for TESTS and diagnostics only (e.g. 'where in the picture is the true banana?').
        Returns (x, y) on the 0..1000 scale of what the model sees, or None when the point is behind the camera."""
        p = np.array([wx, wy, wz]) - frame.cam_pos
        cam = frame.cam_mat.T @ p
        if cam[2] > -1e-6:
            return None
        px = self.W / 2 + self.f_pix * cam[0] / -cam[2]
        py = self.H / 2 - self.f_pix * cam[1] / -cam[2]
        return self.pixel_to_view(px, py)
