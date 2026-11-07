"""recorder.py - saves the MuJoCo footage of a run as an mp4, recorded while the test runs.

The video is ONE MuJoCo view, zoomed so that the table fills the picture (1280 x 720, nothing drawn on top of it), with a
slim dark footer under it (1280 x 96) that shows the same text as the MuJoCo window caption (scene, step and tool, last
result), the running time and the number of tool calls - and, when a scene is over, SUCCESSFUL / NOT SUCCESSFUL, the
completion time, the tool calls and the LLM thinking time.

One frame is drawn every 1/fps seconds of SIMULATED time, so the video plays at real speed and is smooth even when the
window or the LLM is slow. The time the LLM spends thinking is not in the video (the simulation stands still then); it is
written in the footer at the end of the scene. It works with or without the MuJoCo window (--view), and it needs no
change in duck_sim.py: it hooks into the duck's _carry() step, which runs after every physics step.

Video writer: imageio (pip install imageio imageio-ffmpeg) or, if that is missing, OpenCV (pip install opencv-python).
Text: Pillow (installed together with imageio; otherwise pip install pillow).
"""

import os

import mujoco
import numpy as np

from sim.duck_sim import DT_PHYS

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                                             # the video is then saved without the footer text
    Image = None

W, H, FOOT = 1280, 720, 96                                      # the MuJoCo picture is W x H, the footer W x FOOT under it
# zoomed to the table (the corners of the table top and the duck's head above them just fit): (look at, distance m,
# azimuth deg, elevation deg). The MuJoCo window itself is not changed.
CAMERA = ((0.115, 0.067, 0.211), 1.15, 150, -34)

BG, WHITE, YELLOW, GREY = (22, 27, 36), (255, 255, 255), (255, 214, 64), (170, 180, 195)
GREEN, RED, NEUTRAL = (30, 140, 65), (185, 45, 45), (48, 56, 70)


class _ImageioWriter:
    def __init__(self, path, fps):
        import imageio
        self.w = imageio.get_writer(path, fps=fps, codec="libx264", quality=7, pixelformat="yuv420p", macro_block_size=1)

    def add(self, frame):
        self.w.append_data(frame)

    def close(self):
        self.w.close()


class _Cv2Writer:
    def __init__(self, path, fps):
        import cv2
        self.cv2 = cv2
        self.w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H + FOOT))
        if not self.w.isOpened():
            raise RuntimeError("OpenCV could not open the video file")

    def add(self, frame):
        self.w.write(self.cv2.cvtColor(frame, self.cv2.COLOR_RGB2BGR))

    def close(self):
        self.w.release()


def _open_writer(path, fps):
    errors = []
    for cls in (_ImageioWriter, _Cv2Writer):
        try:
            return cls(path, fps)
        except Exception as e:                                  # not installed, or no ffmpeg inside it
            errors.append(f"{cls.__name__[1:-6]}: {e}")
    raise SystemExit("Cannot save video: no video writer works here.\n  " + "\n  ".join(errors) +
                     "\nInstall one and run again:   pip install imageio imageio-ffmpeg")


def _font(size, bold=False):
    names = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _fit(draw, text, font, width):
    """Cut a text that is too wide and end it with '...'."""
    if draw.textlength(text, font=font) <= width:
        return text
    while len(text) > 8 and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


class Recorder:
    def __init__(self, duck, out_dir, name, fps=30):
        self.duck, self.fps = duck, int(fps)
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, name + ".mp4")
        g = duck.model.vis.global_                                # the off-screen picture must fit into the model's buffer
        g.offwidth, g.offheight = max(g.offwidth, W), max(g.offheight, H)
        self.rd = mujoco.MjData(duck.model)                       # a copy of the state, so the simulation itself is never touched
        self.r = mujoco.Renderer(duck.model, height=H, width=W)
        self._give_window_back()
        look, dist, az, el = CAMERA
        self.cam = mujoco.MjvCamera()
        self.cam.lookat[:] = look
        self.cam.distance, self.cam.azimuth, self.cam.elevation = dist, az, el
        if Image is not None:
            self.f_title, self.f_small = _font(22, True), _font(18)
            self.f_num, self.f_verdict, self.f_note = _font(28, True), _font(30, True), _font(20)
        else:
            print("[recorder] Pillow is not installed: the video is saved WITHOUT the footer text.   pip install pillow", flush=True)
        self.writer = _open_writer(self.path, self.fps)
        self.n_phys = self.n_frames = 0
        self.marks = []                                           # [seconds into the video, text]: where each scene starts
        self.caption, self.steps, self.verdict = [], 0, None
        self._orig_carry = duck._carry
        duck._carry = self._carry                                 # runs after every 5 ms physics step
        print(f"[recorder] saving footage to {self.path}  ({W}x{H + FOOT}, {self.fps} frames per second)", flush=True)

    # ---- what the benchmark tells the recorder (the text in the footer) ----
    def mark(self, text):
        """A new scene starts: remember where in the video (written to the .txt) and clear the text of the last scene."""
        self.marks.append([self.n_frames / self.fps, text])
        self.caption, self.steps, self.verdict = [], 0, None

    def set_caption(self, lines, steps):
        """The same lines as the caption of the MuJoCo window: scene / step and tool / last result."""
        if self.verdict is None:
            self.caption, self.steps = list(lines), steps

    def set_verdict(self, success, seconds, steps, llm_seconds, ended):
        """The scene is over: the footer shows the verdict. Also written into the .txt."""
        self.verdict = dict(success=bool(success), seconds=float(seconds), steps=int(steps), llm=float(llm_seconds), ended=str(ended))
        if self.marks:
            self.marks[-1][1] += f"   ->  {'SUCCESSFUL' if success else 'NOT SUCCESSFUL'}, {int(steps)} tool calls, {float(seconds):.1f} s"

    # ---- recording ----
    def _give_window_back(self):
        """If the MuJoCo window is open, give it its drawing context back (this picture used another one)."""
        v = getattr(self.duck, "viewer", None)
        if v is not None and getattr(self.duck, "_fast", False):
            v.glfw.make_context_current(v.win)

    def _carry(self):
        self._orig_carry()
        self.n_phys += 1
        if int(self.n_phys * DT_PHYS * self.fps) > self.n_frames:    # time for the next frame (simulated time)
            self._frame()

    def _frame(self):
        d = self.duck
        self.rd.qpos[:] = d.data.qpos
        self.rd.qvel[:] = d.data.qvel
        mujoco.mj_forward(d.model, self.rd)
        self.r.update_scene(self.rd, camera=self.cam)
        pic = self.r.render()
        self._give_window_back()
        frame = np.empty((H + FOOT, W, 3), np.uint8)
        frame[:H] = pic
        frame[H:] = BG
        if Image is not None:
            frame[H:] = self._footer()
        self.writer.add(frame)
        self.n_frames += 1

    def _footer(self):
        img = Image.new("RGB", (W, FOOT), BG)
        dr = ImageDraw.Draw(img)
        v = self.verdict
        # left: the lines of the window caption - scene (white), step and tool (yellow), last result (grey)
        for text, font, color, y in zip(self.caption[:3], (self.f_title, self.f_title, self.f_small), (WHITE, YELLOW, GREY), (7, 35, 66)):
            dr.text((20, y), _fit(dr, text, font, 800), font=font, fill=color)
        # right: time and tool calls while the scene runs; the verdict when it is over
        x0, x1, y0, y1 = 840, W - 14, 8, FOOT - 8
        dr.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=(GREEN if v["success"] else RED) if v else NEUTRAL)
        cx = (x0 + x1) / 2
        if v:
            rows = [("SUCCESSFUL" if v["success"] else "NOT SUCCESSFUL", self.f_verdict, 1),
                    (f"Completion time  {v['seconds']:.1f} s", self.f_note, 36),
                    (f"{v['steps']} tool calls   -   LLM thinking {v['llm']:.1f} s", self.f_note, 57)]
        else:
            t = self.duck.t if self.steps else 0.0
            rows = [(f"Time  {t:5.1f} s", self.f_num, 5), (f"Tool calls  {self.steps}", self.f_num, 38)]
        for text, font, y in rows:
            text = _fit(dr, text, font, x1 - x0 - 16)
            dr.text((cx - dr.textlength(text, font=font) / 2, y0 + y), text, font=font, fill=WHITE)
        return np.asarray(img)

    def close(self):
        self.duck._carry = self._orig_carry
        self.writer.close()
        self.r.close()
        seconds = self.n_frames / self.fps
        with open(os.path.splitext(self.path)[0] + ".txt", "w", encoding="utf-8") as f:
            f.write(f"{os.path.basename(self.path)}   {int(seconds // 60)}:{seconds % 60:04.1f} long, {self.n_frames} frames at {self.fps} fps\n")
            f.write("where each scene starts in the video, and how it ended:\n")
            for t, text in self.marks:
                f.write(f"  {int(t // 60):02d}:{t % 60:04.1f}   {text}\n")
        print(f"[recorder] saved {self.path}  ({int(seconds // 60)}:{seconds % 60:04.1f} of footage)", flush=True)