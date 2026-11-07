"""duck_sim.py - Franky's body.

Official Microduck MuJoCo model + Pollen's pretrained ONNX policies, driven from
Python code (no keyboard, so it runs on Windows). Adds a table, a banana and a
green box to the official scene.

Low-level skills here (the LLM agent will call these later, never the motors):
    hold(s)                 stand still
    walk(vx, vy, wz, s)     walk with a velocity command
    ground_pick(...)        crouch, mouth tip touches the table, stand back up
                            (simulated grasp / release happens at the bottom)

SIMULATED GRASP: the official simulation has no actuated jaw, so while the banana
is "held" it is attached to the mouth_tip site by code (collisions off), and
released on command. Say this clearly in any write-up.
The banana is held CROSSWISE, like a bar across the face: it lies ACROSS the beak (its long side
perpendicular to the way the beak points), its middle on the beak tip, sticking out equally to the left and right.
That is the stable way to carry it (the weight is balanced on the beak, nothing levers the head down). So a grasp only
works when the banana lies across the beak (within PICK_ANGLE_TOL of exactly 90 deg), and the duck has to come at the
banana from its SIDE. HOLD_OFFSET below is the one switch for this (0 = the old lengthwise grip).

Policy contract copied from microduck_rl/scripts/infer_policy.py:
    obs (61) = gyro(3) + projected gravity(3) + joint_pos-default(14)
               + joint_vel(14) + last_action(14) + command(13)
    action (14) -> motor target = DEFAULT_POSE + action ; 50 Hz = 4 x 5 ms
"""

from __future__ import annotations

import math
import os
import time

import mujoco
import numpy as np
import onnxruntime as ort

from project_paths import POLICY_DIR, ROBOT_DIR       # where the robot model and the ONNX policies are (project_paths.py)

SCENE_XML = os.path.join(ROBOT_DIR, "scene.xml")

# --- BAM servo model (must mirror infer_policy.py / training) -----------------
BAM_MOTOR, BAM_MODEL, BAM_KP_FW, BAM_VIN, BAM_VIN_DROP_GAIN, BAM_VIN_MIN = "xl330", "m6", 200.0, 7.4, 0.1, 6.0
BAM_SOLREF_FRICTION = (-5.0e4, -2.0e2)
BAM_SOLIMP_FRICTION = (0.99, 0.9999, 0.001, 0.5, 2.0)

DT_PHYS, DECIM = 0.005, 4
DT_CTRL = DT_PHYS * DECIM  # 0.02 s = 50 Hz

DEFAULT_POSE = np.array([
    0.0, -0.0873, -0.4579, -0.0049, 0.4530,   # left leg
    0.3491, 0.3491, 0.0, 0.0,                 # neck_pitch, head_pitch, head_yaw, head_roll
    0.0, 0.0873, 0.4579, 0.0049, -0.4530,     # right leg
], dtype=np.float32)

# --- World layout --------------------------------------------------------------
TABLE_H = 0.30            # table-top height (m)
TABLE_HALF = (0.60, 0.50)  # table-top half size x, y (m)
PAD_HALF = 0.07           # green box inner half size (m)
GRASP_RADIUS = 0.05       # mouth-to-banana distance that counts as a grasp (m)
GRASP_PHASE = 0.30        # ground_pick phase where the beak is lowest (measured)
HOLD_OFFSET = math.pi / 2  # rad: the held banana's long side is turned this far from the beak direction (pi/2 = ACROSS the beak)
PICK_ANGLE_TOL = 20.0     # deg: the banana may be at most this far from lying exactly across the beak for a grasp to work
RELEASE_FORWARD = 0.03    # m: a released banana is put this far in front of the beak tip, so the beak does not drag it back
#                           when the duck stands up (a banana lying across the beak was dragged ~5 cm backwards otherwise)
APPROACH_LEAD = 0.12      # m: look-ahead of the final leg (bigger = heading counts more than the sideways error)
APPROACH_GAIN = 1.2       # steering gain of the final leg (rad/s of turn per rad of heading error)
BOX_CLEAR = 0.16          # m: keep the trunk this far from the box centre while walking (the tray rim is a trip hazard)
BANANA_CLEAR = 0.11       # m: do not walk through the banana on the way to the staging point
STANDOFF = 0.095          # trunk-to-target distance where the beak lands on the target (see walk_to)


def _rot_inv(q, v):
    """Rotate v by the inverse of quaternion q=[w,x,y,z]."""
    w, xyz = q[0], q[1:4]
    t = np.cross(xyz, v) * 2
    return v - w * t + np.cross(xyz, t)


HIST_N = 16   # physics states kept for drawing (16 x 5 ms = 80 ms)


class ViewerClosed(Exception):
    """Raised inside Duck.tick() when the window is closed (or ESC is pressed)."""


class _FastViewer:
    """A MuJoCo window that lives in the SAME thread as the physics and draws once per screen refresh.

    Why: the official passive viewer renders in its own thread and viewer.sync() fights it for a lock.
    Measured on this PC: ~9 ms per sync() and spikes of 300 ms -> the picture stutters even though the
    physics is fast. Here there is no second thread, no lock, and the swap waits for the screen's
    vertical sync, so every refresh gets a new picture. Mouse: left drag = rotate, right drag = move,
    wheel = zoom (Shift swaps the drag direction). ESC or the window's X = stop.
    """

    def __init__(self, model, title, lookat, distance, azimuth, elevation, size=(1280, 720)):
        import glfw
        self.glfw, self.model, self.win, self.closed, self.inited = glfw, model, None, False, False
        if not glfw.init():
            raise RuntimeError("glfw.init() failed")
        self.inited = True
        try:
            glfw.window_hint(glfw.SAMPLES, 4)
            self.win = glfw.create_window(size[0], size[1], title, None, None)
            if not self.win:
                raise RuntimeError("could not open a window")
            glfw.make_context_current(self.win)
            glfw.swap_interval(1)                      # wait for the screen's vertical sync
            self.scn = mujoco.MjvScene(model, maxgeom=2000)
            self.cam = mujoco.MjvCamera()
            self.opt = mujoco.MjvOption()
            self.pert = mujoco.MjvPerturb()
            self.con = mujoco.MjrContext(model, mujoco.mjtFontScale.mjFONTSCALE_150.value)
        except Exception:
            self.close()
            raise
        self.cam.lookat[:] = lookat
        self.cam.distance, self.cam.azimuth, self.cam.elevation = distance, azimuth, elevation
        self.cam2, self.view2 = mujoco.MjvCamera(), 0           # second picture (right half); C = next view
        self.trunk = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "trunk_base")
        self.caption = ""                                       # text shown at the bottom of the left picture
        rate = 0
        try:
            rate = glfw.get_video_mode(glfw.get_primary_monitor()).refresh_rate
        except Exception:
            pass
        self.rate = rate if 30 <= rate <= 360 else 60
        self.P = 1.0 / self.rate                       # one screen refresh (s)
        self.f_last = time.perf_counter()              # when the previous picture hit the screen
        self.next_ok = 0.0
        self.fps = float(self.rate)
        self.n = self.late = self.pauses = 0
        self.dt_sum = self.dt_max = self.draw_sum = self.draw_max = 0.0
        self._btn = [False, False, False]
        self._xy = [0.0, 0.0]
        glfw.set_cursor_pos_callback(self.win, self._on_move)
        glfw.set_mouse_button_callback(self.win, self._on_button)
        glfw.set_scroll_callback(self.win, self._on_scroll)
        glfw.set_key_callback(self.win, self._on_key)

    # --- mouse / keyboard
    def _on_button(self, win, button, action, mods):
        if button in (0, 1, 2):
            self._btn[button] = action != self.glfw.RELEASE
        self._xy[:] = self.glfw.get_cursor_pos(win)

    def _on_move(self, win, x, y):
        dx, dy = x - self._xy[0], y - self._xy[1]
        self._xy[:] = [x, y]
        left, right, middle = self._btn
        if not (left or right or middle):
            return
        g, M = self.glfw, mujoco.mjtMouse
        shift = g.get_key(win, g.KEY_LEFT_SHIFT) == g.PRESS or g.get_key(win, g.KEY_RIGHT_SHIFT) == g.PRESS
        if right:
            act = M.mjMOUSE_MOVE_H if shift else M.mjMOUSE_MOVE_V
        elif left:
            act = M.mjMOUSE_ROTATE_H if shift else M.mjMOUSE_ROTATE_V
        else:
            act = M.mjMOUSE_ZOOM
        h = max(1, g.get_window_size(win)[1])
        self._move_camera(act, dx / h, dy / h)

    def _move_camera(self, act, rx, ry):
        try:
            mujoco.mjv_moveCamera(self.model, act, rx, ry, self.cam)
        except TypeError:                               # older MuJoCo versions also want the scene
            mujoco.mjv_moveCamera(self.model, act, rx, ry, self.scn, self.cam)

    def _on_scroll(self, win, xoff, yoff):
        self._move_camera(mujoco.mjtMouse.mjMOUSE_ZOOM, 0.0, -0.05 * yoff)

    def _on_key(self, win, key, scancode, action, mods):
        if key == self.glfw.KEY_ESCAPE and action == self.glfw.PRESS:
            self.closed = True
        if key == self.glfw.KEY_C and action == self.glfw.PRESS:
            self.view2 = (self.view2 + 1) % len(self.VIEW2)

    # --- drawing: TWO pictures side by side. Left = your camera (mouse), right = a second camera (press C to change it)
    VIEW2 = [   # (title, point to look at - None = follow the duck, distance in m, azimuth deg, elevation deg)
        ("TOP VIEW",  (0.0, 0.0, TABLE_H),        1.80, 90, -90),
        ("CLOSE-UP",  None,                       0.55, 140, -22),
        ("SIDE VIEW", (0.0, 0.0, TABLE_H + 0.05), 1.70, 90, -6),
    ]

    def _render_views(self, d, sim_time, speed, w, h):
        half = w // 2
        left, right = mujoco.MjrRect(0, 0, half, h), mujoco.MjrRect(half, 0, w - half, h)
        title, look, dist, az, el = self.VIEW2[self.view2]
        c2 = self.cam2
        c2.lookat[:] = d.xpos[self.trunk] if look is None else look
        c2.distance, c2.azimuth, c2.elevation = dist, az, el
        for cam, rect in ((self.cam, left), (c2, right)):
            mujoco.mjv_updateScene(self.model, d, self.opt, self.pert, cam, mujoco.mjtCatBit.mjCAT_ALL.value, self.scn)
            mujoco.mjr_render(rect, self.scn, self.con)
        mujoco.mjr_rectangle(mujoco.MjrRect(half - 1, 0, 2, h), 0.0, 0.0, 0.0, 1.0)      # thin divider
        font, corner = mujoco.mjtFont.mjFONT_NORMAL.value, mujoco.mjtGridPos.mjGRID_TOPLEFT.value
        names = "Time\nFPS" + ("\nSpeed" if speed != 1.0 else "")
        values = f"{sim_time:.2f} s\n{self.fps:.0f}" + (f"\nx{speed:g}" if speed != 1.0 else "")
        mujoco.mjr_overlay(font, corner, left, names, values, self.con)
        mujoco.mjr_overlay(font, corner, right, title, "C = next view", self.con)
        if self.caption:
            mujoco.mjr_overlay(font, mujoco.mjtGridPos.mjGRID_BOTTOMLEFT.value, left, self.caption, "", self.con)

    def draw(self, d, sim_time, speed):
        """Draw MjData `d`, wait for the vertical sync, remember when the picture hit the screen."""
        g, pc = self.glfw, time.perf_counter
        r0 = pc()
        w, h = g.get_framebuffer_size(self.win)
        if w > 0 and h > 0:                            # (0 x 0 = minimised window)
            self._render_views(d, sim_time, speed, w, h)
        r1 = pc()
        g.swap_buffers(self.win)                       # <- blocks until the next screen refresh
        f = pc()
        g.poll_events()
        if g.window_should_close(self.win):
            self.closed = True
        dt = f - self.f_last
        self.f_last = f
        # next picture: shortly after this refresh if the swap really waited for the screen (vsync works),
        # otherwise one refresh period after this picture started (frame-rate limiter)
        self.next_ok = f + 0.2 * self.P if f - r1 > 0.25 * self.P else r0 + self.P
        if dt > 0.5:                                   # the program paused (not a display problem)
            self.pauses += 1
        else:
            self.n += 1
            self.dt_sum += dt
            self.dt_max = max(self.dt_max, dt)
            self.late += dt > 1.5 * self.P
            self.fps += 0.05 * (1.0 / max(dt, 1e-3) - self.fps)
            self.draw_sum += r1 - r0
            self.draw_max = max(self.draw_max, r1 - r0)

    def report(self):
        n = max(1, self.n)
        return (f"display ({self.rate} Hz screen): {self.n} frames, gap avg {1000*self.dt_sum/n:.1f} ms "
                f"(worst {1000*self.dt_max:.1f}), late frames {self.late} ({100*self.late/n:.1f}%), "
                f"drawing avg {1000*self.draw_sum/n:.2f} ms (worst {1000*self.draw_max:.1f})")

    def close(self):
        g = self.glfw
        if getattr(self, "con", None) is not None:
            try:
                self.con.free()
            except Exception:
                pass
            self.con = None
        if self.win:
            try:
                g.destroy_window(self.win)
            except Exception:
                pass
            self.win = None
        if self.inited:
            self.inited = False
            try:
                g.terminate()
            except Exception:
                pass


class Duck:
    def __init__(self, name="Franky", viewer=False, realtime=True, speed=1.0):
        """speed: 1.0 = real time, 0.5 = slow motion (easier to watch)."""
        self.name = name
        self.speed = speed
        self.stats = dict(ticks=0, work_sum=0.0, work_max=0.0, sync_sum=0.0, sync_max=0.0)
        self._pt, self._wall0, self._pt0 = 0.0, None, 0.0       # physics clock <-> wall clock (see _pace)
        if os.name == 'nt':
            try:  # ask Windows for 1 ms timer resolution (default 15.6 ms makes sleeps coarse)
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass
        self.realtime = realtime and viewer
        self._build_model()

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1      # 61->14 MLP: extra threads only add latency/jitter
        so.inter_op_num_threads = 1
        mk = lambda f: ort.InferenceSession(os.path.join(POLICY_DIR, f), so, providers=["CPUExecutionProvider"])
        self.walk_sess = mk(os.environ.get("DUCK_WALK_POLICY", "velstand.onnx"))
        self.pick_sess = mk("alpha_ground_pick.onnx")
        self.sess = self.walk_sess
        self.in_name = self.walk_sess.get_inputs()[0].name
        self.out_name = self.walk_sess.get_outputs()[0].name

        # recent physics states, so a picture can be drawn for ANY moment (not only on 5 ms physics steps)
        self.rdata = mujoco.MjData(self.model)
        self._hq = np.zeros((HIST_N, self.model.nq))
        self._ht = np.zeros(HIST_N)
        self._hn = 0
        self._quat_adr = [self.free_q + 3, self.ban_q + 3]       # free-joint quaternions inside qpos

        self.viewer, self._fast = None, False
        self.reset(seed=0)
        if viewer:
            self._open_viewer()

    def _open_viewer(self):
        """Fast single-thread window by default; the official passive viewer if that cannot open
        (or if the environment variable DUCK_VIEWER=passive is set)."""
        lookat, dist, az, el = [0.0, 0.0, TABLE_H], 1.2, 150, -25
        if os.environ.get("DUCK_VIEWER", "fast") != "passive":
            try:
                self.viewer = _FastViewer(self.model, f"{self.name} - MuJoCo", lookat, dist, az, el)
                self._fast = True
                return
            except Exception as e:
                print(f"[duck_sim] fast window not available ({e}); using the standard MuJoCo viewer (may stutter)")
        import mujoco.viewer
        self.viewer = mujoco.viewer.launch_passive(self.model, self.data)
        self.viewer.cam.lookat[:] = lookat
        self.viewer.cam.distance, self.viewer.cam.azimuth, self.viewer.cam.elevation = dist, az, el
        self.viewer.sync()

    # ------------------------------------------------------------------ model
    def _build_model(self):
        from bam.model import load_model
        from bam.mujoco import MujocoController

        bam_model = load_model(motor_name=BAM_MOTOR, model=BAM_MODEL)
        bam_model.actuator.kp = BAM_KP_FW
        bam_model.actuator.vin = BAM_VIN
        bam_model.actuator.max_current = None

        spec = mujoco.MjSpec.from_file(SCENE_XML)
        self._add_world(spec)

        force_limit = bam_model.actuator.vin * bam_model.kt.value / bam_model.R.value
        names = []
        for act in spec.actuators:
            tgt = act.target.name if hasattr(act.target, "name") else str(act.target)
            if tgt.startswith("passive_"):
                continue
            act.set_to_motor()
            act.forcelimited = True
            act.forcerange = (-force_limit, force_limit)
            act.ctrllimited = False
            act.gear = [1.0, 0, 0, 0, 0, 0]
            names.append(act.name)
            for joint in spec.joints:
                if joint.name == tgt:
                    joint.damping = np.zeros((3, 1))
                    joint.frictionloss = 0.0
                    joint.solref_friction = BAM_SOLREF_FRICTION
                    joint.solimp_friction = BAM_SOLIMP_FRICTION
                    break

        self.model = spec.compile()
        self.model.opt.timestep = DT_PHYS
        self.data = mujoco.MjData(self.model)
        self.bam = MujocoController(bam_model, names, self.model, self.data,
                                    vin_drop_gain=BAM_VIN_DROP_GAIN, vin_min=BAM_VIN_MIN)

        m = self.model
        nm = lambda t, n: mujoco.mj_name2id(m, t, n)
        O = mujoco.mjtObj
        self.trunk_id = nm(O.mjOBJ_BODY, "trunk_base")
        self.gyro_adr = int(m.sensor_adr[nm(O.mjOBJ_SENSOR, "imu_ang_vel")])
        self.mouth_id = nm(O.mjOBJ_SITE, "mouth_tip")
        fj = nm(O.mjOBJ_JOINT, "trunk_base_freejoint")
        self.free_q = int(m.jnt_qposadr[fj])
        self.free_v = int(m.jnt_dofadr[fj])
        self.q_idx = [int(m.jnt_qposadr[m.actuator_trnid[i, 0]]) for i in range(m.nu)]
        self.v_idx = [int(m.jnt_dofadr[m.actuator_trnid[i, 0]]) for i in range(m.nu)]
        bj = nm(O.mjOBJ_JOINT, "banana_free")
        self.ban_q, self.ban_v = int(m.jnt_qposadr[bj]), int(m.jnt_dofadr[bj])
        self.ban_body = nm(O.mjOBJ_BODY, "banana")
        self.ban_geom = nm(O.mjOBJ_GEOM, "banana_geom")
        self.box_body = nm(O.mjOBJ_BODY, "green_box")

    def _add_world(self, spec):
        """Table + banana + green box, added to the official scene."""
        G = mujoco.mjtGeom
        wb = spec.worldbody
        # table (top collides; legs are visual only)
        t = wb.add_body(name="table", pos=[0, 0, 0])
        t.add_geom(name="table_top", type=G.mjGEOM_BOX, size=[TABLE_HALF[0], TABLE_HALF[1], 0.02],
                   pos=[0, 0, TABLE_H - 0.02], rgba=[0.62, 0.45, 0.28, 1], friction=[1.0, 0.005, 0.0001])
        for sx in (-1, 1):
            for sy in (-1, 1):
                t.add_geom(type=G.mjGEOM_BOX, size=[0.02, 0.02, (TABLE_H - 0.04) / 2], contype=0, conaffinity=0,
                           pos=[sx * (TABLE_HALF[0] - 0.04), sy * (TABLE_HALF[1] - 0.04), (TABLE_H - 0.04) / 2],
                           rgba=[0.45, 0.32, 0.2, 1])
        # banana: a yellow capsule lying on the table
        b = wb.add_body(name="banana", pos=[0.2, 0, TABLE_H + 0.01])
        b.add_freejoint(name="banana_free")
        b.add_geom(name="banana_geom", type=G.mjGEOM_CAPSULE, size=[0.009, 0.028, 0], quat=[0.7071, 0, 0.7071, 0],
                   mass=0.01, rgba=[1.0, 0.85, 0.1, 1], friction=[0.8, 0.005, 0.0001])
        # green box: shallow tray (low rim so the duck's head can reach over it)
        gb = wb.add_body(name="green_box", pos=[0.2, 0.2, TABLE_H])
        gb.add_geom(name="box_floor", type=G.mjGEOM_BOX, size=[PAD_HALF + 0.012, PAD_HALF + 0.012, 0.003],
                    pos=[0, 0, 0.003], rgba=[0.1, 0.65, 0.2, 1])
        for nm_, p, s in (("w_n", [0, PAD_HALF + 0.006, 0.008], [PAD_HALF + 0.012, 0.006, 0.008]),
                          ("w_s", [0, -PAD_HALF - 0.006, 0.008], [PAD_HALF + 0.012, 0.006, 0.008]),
                          ("w_e", [PAD_HALF + 0.006, 0, 0.008], [0.006, PAD_HALF + 0.012, 0.008]),
                          ("w_w", [-PAD_HALF - 0.006, 0, 0.008], [0.006, PAD_HALF + 0.012, 0.008])):
            gb.add_geom(name=nm_, type=G.mjGEOM_BOX, size=s, pos=p, rgba=[0.1, 0.65, 0.2, 1])

    # ------------------------------------------------------------------ reset
    def reset(self, seed=0, duck_xy=(-0.40, 0.0), duck_yaw=0.0, banana_xy=None, box_xy=None, banana_yaw=0.0):
        """Put everything back. Positions are random from `seed` unless given. banana_yaw (rad) = which way the
        banana's long side points in the world (0 = along the table's x axis)."""
        rng = np.random.default_rng(seed)
        side = rng.choice([-1.0, 1.0])
        if banana_xy is None:
            banana_xy = (rng.uniform(0.10, 0.25), side * rng.uniform(0.08, 0.20))
        if box_xy is None:
            box_xy = (rng.uniform(0.10, 0.25), -side * rng.uniform(0.15, 0.25))
        m, d = self.model, self.data
        mujoco.mj_resetData(m, d)
        q = self.free_q
        d.qpos[q:q + 3] = [duck_xy[0], duck_xy[1], TABLE_H + 0.125]
        d.qpos[q + 3:q + 7] = [math.cos(duck_yaw / 2), 0, 0, math.sin(duck_yaw / 2)]
        for i, qi in enumerate(self.q_idx):
            d.qpos[qi] = DEFAULT_POSE[i]
        d.qpos[self.ban_q:self.ban_q + 3] = [banana_xy[0], banana_xy[1], TABLE_H + 0.011]
        d.qpos[self.ban_q + 3:self.ban_q + 7] = [math.cos(banana_yaw / 2), 0, 0, math.sin(banana_yaw / 2)]
        m.body_pos[self.box_body] = [box_xy[0], box_xy[1], TABLE_H]
        m.geom_contype[self.ban_geom] = 1
        m.geom_conaffinity[self.ban_geom] = 1
        self.holding = False
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
        self.seed, self.banana_start, self.box_xy = seed, tuple(banana_xy), tuple(box_xy)
        # let it settle (standing, no command)
        self.tick(25)
        self.t = 0.0

    # ----------------------------------------------------------- core control
    def _obs(self):
        d = self.data
        quat = d.xquat[self.trunk_id]
        grav = _rot_inv(quat, np.array([0.0, 0.0, -1.0]))
        cmd = np.zeros(13, dtype=np.float32)
        if self.pick_phase is not None:
            cmd[0], cmd[1] = math.cos(2 * math.pi * self.pick_phase), math.sin(2 * math.pi * self.pick_phase)
        else:
            cmd[0:3] = self.twist
        return np.concatenate([
            d.sensordata[self.gyro_adr:self.gyro_adr + 3], grav,
            d.qpos[self.q_idx] - DEFAULT_POSE, d.qvel[self.v_idx], self.last_action, cmd,
        ]).astype(np.float32)

    def _carry(self):
        """Simulated grasp: while held, the banana rides on the mouth tip."""
        if self.holding:
            d = self.data
            yaw = self.pose()[2] + HOLD_OFFSET          # across the beak, its middle on the mouth tip
            d.qpos[self.ban_q:self.ban_q + 3] = d.site_xpos[self.mouth_id]
            d.qpos[self.ban_q + 3:self.ban_q + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
            d.qvel[self.ban_v:self.ban_v + 6] = 0.0

    def tick(self, n=1):
        """Run n control ticks (20 ms each = 4 physics steps of 5 ms)."""
        pc = time.perf_counter
        for _ in range(n):
            if self.pick_phase is not None:
                self.pick_phase += DT_CTRL / 4.0          # period 4 s
                if self.pick_phase >= 0.7:                 # episode over -> back to walk/stand policy
                    self.pick_phase, self.sess, self.twist[:] = None, self.walk_sess, 0.0
            w0 = pc()
            obs = self._obs().reshape(1, -1)
            act = self.sess.run([self.out_name], {self.in_name: obs})[0].squeeze(0).astype(np.float32)
            self.last_action = act
            self.bam.q_target[:] = DEFAULT_POSE + act
            work = pc() - w0
            sync_t = 0.0
            for k in range(DECIM):
                w1 = pc()
                self.bam.update()
                mujoco.mj_step(self.model, self.data)
                self._carry()
                work += pc() - w1
                if self.viewer is not None:
                    self._pt += DT_PHYS
                    if self._fast:
                        self._hist_push()
                        if self.realtime:
                            self._pace_and_draw()
                        elif time.perf_counter() >= self.viewer.next_ok:
                            self._draw(self._pt)           # not real-time: show the newest state
                    elif k % 2 == 1:                       # standard viewer (fallback): sync every 10 ms
                        s0 = pc()
                        self.viewer.sync()
                        sync_t += pc() - s0
                        if self.realtime:
                            self._pace()
            self.t += DT_CTRL
            st = self.stats
            st["ticks"] += 1
            st["work_sum"] += work
            st["work_max"] = max(st["work_max"], work)
            st["sync_sum"] += sync_t
            st["sync_max"] = max(st["sync_max"], sync_t)

    def timing_report(self):
        st = self.stats
        n = max(1, st["ticks"])
        s = f"physics + policy: avg {1000*st['work_sum']/n:.2f} ms per 20 ms tick (worst {1000*st['work_max']:.2f}) over {st['ticks']} ticks"
        if self._fast:
            s += "\n" + self.viewer.report()
        elif self.viewer is not None:
            s += f"\nstandard viewer sync: avg {1000*st['sync_sum']/n:.2f} ms per tick (worst {1000*st['sync_max']:.2f})"
        return s

    # --- real-time pacing and drawing -------------------------------------------------------------
    def _pace(self):
        """Sleep until the wall clock catches up with the physics clock (absolute deadlines, so small
        sleep errors never add up). time.sleep only: a busy-wait would just burn the CPU."""
        now = time.perf_counter()
        if self._wall0 is None or now > self._wall0 + (self._pt - self._pt0) / self.speed + 0.5:
            self._wall0, self._pt0 = now, self._pt          # first call, or we fell far behind: restart the clock
            return
        wait = self._wall0 + (self._pt - self._pt0) / self.speed - now
        if wait > 0:
            time.sleep(wait)

    def _hist_push(self):
        i = self._hn % HIST_N
        self._hq[i] = self.data.qpos
        self._ht[i] = self._pt
        self._hn += 1

    def _load_render_state(self, S):
        """rdata <- the pose the duck had at physics-clock time S, blended between the two physics steps
        around S (so motion is smooth at ANY refresh rate, not only on 5 ms steps)."""
        N, hi = HIST_N, self._hn - 1
        lo = self._hn - min(self._hn, N)
        ht, hq = self._ht, self._hq
        if S >= ht[hi % N]:
            q = hq[hi % N]
        elif S <= ht[lo % N]:
            q = hq[lo % N]
        else:
            j = hi - 1
            while ht[j % N] > S:
                j -= 1
            q0, q1 = hq[j % N], hq[(j + 1) % N]
            a = (S - ht[j % N]) / (ht[(j + 1) % N] - ht[j % N])
            q = q0 + a * (q1 - q0)
            for adr in self._quat_adr:                      # blend rotations (q and -q are the same rotation)
                u, v = q0[adr:adr + 4], q1[adr:adr + 4]
                w = (1 - a) * u + a * (-v if float(u @ v) < 0 else v)
                nrm = float(np.linalg.norm(w))
                q[adr:adr + 4] = w / nrm if nrm > 1e-9 else u
        self.rdata.qpos[:] = q
        mujoco.mj_forward(self.model, self.rdata)

    def _pace_and_draw(self):
        """Called after every 5 ms physics step. Waits until it is time to show that step, and while waiting
        draws a picture whenever the screen is ready for one - at the screen's own moment, not on the 5 ms
        physics grid. The picture shows the pose the duck had when the PREVIOUS picture appeared (a fixed
        ~17 ms behind), so there is always a physics step on both sides of it to blend between."""
        v, pc = self.viewer, time.perf_counter
        now = pc()
        if self._wall0 is None or now > self._wall0 + (self._pt - self._pt0) / self.speed + 0.5:
            self._wall0, self._pt0 = now, self._pt              # first call, or far behind: restart the clock
            return
        while True:
            due = self._wall0 + (self._pt - self._pt0) / self.speed
            now = pc()
            if now >= due:
                return                                          # behind the clock: go and compute the next step
            if v.next_ok < due:                                 # the screen wants a picture before this step is due
                if v.next_ok > now:
                    time.sleep(v.next_ok - now)
                self._draw(min(self._pt0 + (v.f_last - self._wall0) * self.speed, self._pt))
            else:
                time.sleep(due - now)
                return

    def _draw(self, S):
        self._load_render_state(S)
        self.viewer.draw(self.rdata, self.t - (self._pt - S), self.speed)
        if self.viewer.closed:
            raise ViewerClosed("window closed")

    # ----------------------------------------------------------------- skills
    def hold(self, seconds=1.0):
        self.twist[:] = 0.0
        self.tick(int(round(seconds / DT_CTRL)))

    def walk(self, vx=0.0, vy=0.0, wz=0.0, seconds=1.0):
        """Raw velocity command. MEASURED behaviour of velstand.onnx on the table:
        - |vx| < ~0.25 or |wz| < ~0.6 -> the duck just stands (dead-zone)
        - vx 0.4 -> ~0.12 m/s forward ; vx 0.5 -> ~0.23 m/s (can run off the table!)
        - vy (sideways) does not work -> use turn + forward instead
        """
        self.twist[:] = [np.clip(vx, -0.5, 0.5), 0.0, np.clip(wz, -1.5, 1.5)]
        self.tick(int(round(seconds / DT_CTRL)))

    # --- closed-loop navigation skills (use the duck's own pose estimate) -----
    @staticmethod
    def _wrap(a):
        return (a + math.pi) % (2 * math.pi) - math.pi

    def turn_to(self, heading, tol_deg=6.0, max_s=8.0):
        """Turn in place to a world heading (rad). Returns dict(ok, err_deg, seconds)."""
        t0, tol = self.t, math.radians(tol_deg)
        while self.t - t0 < max_s:
            err = self._wrap(heading - self.pose()[2])
            if abs(err) < tol or self.is_fallen():
                break
            wz = math.copysign(min(1.5, max(0.8, 1.5 * abs(err))), err)  # stay above the 0.6 dead-zone
            self.twist[:] = [0.0, 0.0, wz]
            self.tick(5)
        self.hold(0.3)
        err = math.degrees(self._wrap(heading - self.pose()[2]))
        return {"ok": abs(err) <= tol_deg * 1.5, "err_deg": round(err, 1), "seconds": round(self.t - t0, 1)}

    def settle(self, max_s=3.0):
        """Stand still until the duck stops drifting/turning (it keeps moving ~0.5 s after a command ends)."""
        t0 = self.t
        while self.t - t0 < max_s:
            x0, y0, yaw0, _ = self.pose()
            self.hold(0.3)
            x1, y1, yaw1, _ = self.pose()
            if math.hypot(x1 - x0, y1 - y0) < 0.003 and abs(self._wrap(yaw1 - yaw0)) < math.radians(1.0):
                return

    def walk_to(self, target_xy, standoff=0.095, tol=0.012, stop_lead=0.0, max_s=45.0):
        """Walk until the trunk is `standoff` m short of target_xy, facing it. Default standoff = where
        the beak ends up during ground_pick (0.108 m ahead of the trunk, minus ~1.3 cm the trunk shifts back).
        MEASURED: turning in place by a SMALL angle springs back (hips twist, feet don't step), so we only
        turn in place for big errors (>35 deg) and otherwise steer WHILE walking (small wz + vx 0.4).
        The duck keeps drifting a few cm after it is told to stop, so the caller should CHECK where the
        target ended up (see reach()) instead of trusting this."""
        t0 = self.t
        res = {"ok": False, "reason": "timeout"}
        while self.t - t0 < max_s:
            if self.is_fallen() or self.off_table():
                res = {"ok": False, "reason": "fell" if self.is_fallen() else "off_table"}
                break
            x, y, yaw, _ = self.pose()
            dx, dy = target_xy[0] - x, target_xy[1] - y
            dist, bearing = math.hypot(dx, dy), math.atan2(dy, dx)
            err = self._wrap(bearing - yaw)
            if getattr(self, "debug", False):
                print(f"    walk_to t={self.t-t0:5.1f} pos=({x:+.3f},{y:+.3f}) yaw={math.degrees(yaw):+6.1f} dist={dist:.3f} err={math.degrees(err):+6.1f}")
            # MEASURED: after the walk command stops, the duck drifts BACK ~7 cm (stop_lead) while it settles,
            # so we stop early by that amount (OFF by default: the drift only shows after big in-place turns, not after a long straight walk).
            if dist <= standoff - stop_lead + tol:
                res = {"ok": abs(err) <= math.radians(20)}
                break
            if abs(err) > math.radians(35):
                self.turn_to(bearing, tol_deg=10.0)
            else:
                self.twist[:] = [0.4, 0.0, float(np.clip(1.0 * err, -0.7, 0.7))]
                self.tick(5)
        self.twist[:] = 0.0
        self.hold(0.5)
        res.update(seconds=round(self.t - t0, 1), **self.reach(target_xy))
        return res

    # ---- path planning helpers (tiny: a straight line, with a detour around the box / the banana) ----
    @staticmethod
    def _seg_dist(p, a, b):
        p, a, b = np.asarray(p, float), np.asarray(a, float), np.asarray(b, float)
        ab = b - a
        t = 0.0 if float(ab @ ab) < 1e-12 else float(np.clip((p - a) @ ab / (ab @ ab), 0.0, 1.0))
        return float(np.linalg.norm(p - (a + t * ab)))

    @staticmethod
    def _on_table(p, margin=0.08):
        return abs(p[0]) <= TABLE_HALF[0] - margin and abs(p[1]) <= TABLE_HALF[1] - margin

    def _route(self, a, b, avoid, depth=0):
        """Waypoints from a to b (b included, a not) that keep clear of the circles in `avoid` = [(xy, radius)]."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        if depth < 3:
            for o, r in avoid:
                o = np.asarray(o, float)
                if np.linalg.norm(a - o) < r or np.linalg.norm(b - o) < r:
                    continue                                   # already inside (or heading for) that circle: ignore it
                if self._seg_dist(o, a, b) < r:
                    d = (b - a) / max(np.linalg.norm(b - a), 1e-9)
                    n = np.array([-d[1], d[0]])
                    cands = [o + k * n * (r + 0.05) for k in (1, -1)]
                    cands = [w for w in cands if self._on_table(w, 0.05)] or cands
                    w = min(cands, key=lambda w: np.linalg.norm(a - w) + np.linalg.norm(w - b))
                    return self._route(a, w, avoid, depth + 1) + self._route(w, b, avoid, depth + 1)
        return [b]

    def _plan_approach(self, c, axis, box_xy):
        """`axis` = the line (rad, world) the duck must walk along to the banana. Pick which of the two directions on that
        line to come from, and where to stand before the final leg (the staging point).
        Cheapest = shortest walk; the staging point must be on the table, and the walk must not cross the box."""
        here = np.array(self.pose()[:2])
        avoid = [(c, BANANA_CLEAR)] + ([(np.asarray(box_xy, float), BOX_CLEAR)] if box_xy is not None else [])
        best = None
        for sgn in (1.0, -1.0):
            u = sgn * np.array([math.cos(axis), math.sin(axis)])
            L = 0.35                                            # runway for lining up; pulled in if it would be off the table
            while L > 0.22 and not self._on_table(c - L * u):
                L -= 0.02
            Q = c - L * u
            S = c - STANDOFF * u
            cost = 3.0 * (0.35 - L)
            if not self._on_table(Q):
                cost += 10.0                                        # staging point off the table: last resort
            if box_xy is not None:
                cost += 20.0 * max(0.0, BOX_CLEAR - self._seg_dist(box_xy, Q, S))   # final leg would cross the box
            way = self._route(here, Q, avoid)
            pts = [here] + way
            cost += sum(float(np.linalg.norm(pts[i + 1] - pts[i])) for i in range(len(pts) - 1))
            if best is None or cost < best[0]:
                best = (cost, u, Q, way)
        return best[1], best[2], best[3]

    def approach_banana(self, c_xy, axis=None, box_xy=None, max_s=60.0):
        """Walk so that the beak ends up ON the middle of the banana AND the banana lies ACROSS the beak (HOLD_OFFSET,
        PICK_ANGLE_TOL), i.e. the duck comes at the banana from its SIDE.
        c_xy = where the banana is (world x, y); axis = which way its long side points (rad, world; None = unknown,
        then simply walk straight at it); box_xy = where the tray is (so we do not walk through it), or None.
        Plan: (1) choose the side of the banana to come from, walk to a staging point ~0.35 m away from it, on the line
                  that is perpendicular to the banana (the "walking line");
              (2) turn in place if still far off that line (small turns spring back, so only big ones);
              (3) walk along the line: the heading is steered onto it and the sideways error is walked off,
                  until the trunk is STANDOFF short of the banana, where the beak lands on its middle."""
        if axis is None:
            return self.walk_to(c_xy, max_s=max_s)
        c = np.array([float(c_xy[0]), float(c_xy[1])])
        t0 = self.t
        res = {"ok": False, "reason": "timeout"}
        walk_line = float(axis) - HOLD_OFFSET                   # the direction the duck must face when it grasps
        for attempt in range(3):                                # the duck drifts after every stop, so check and retry
            u, Q, way = self._plan_approach(c, walk_line, box_xy)
            ang = math.atan2(u[1], u[0])
            n = np.array([-u[1], u[0]])                         # points to the left of the walking direction
            x, y, yaw, _ = self.pose()
            rel = np.array([x, y]) - c
            remaining, side = -float(rel @ u), float(rel @ n)
            lined_up = remaining > 0.20 and abs(side) < 0.04 and abs(self._wrap(ang - yaw)) < math.radians(25)
            if not lined_up:
                for i, w in enumerate(way):
                    last = i == len(way) - 1
                    self.walk_to((float(w[0]), float(w[1])), standoff=0.0, tol=0.03 if last else 0.06, max_s=max_s)
                    if self.is_fallen() or self.off_table():
                        return {"ok": False, "reason": "fell" if self.is_fallen() else "off_table"}
                self.settle()
            if abs(self._wrap(ang - self.pose()[2])) > math.radians(30):
                self.turn_to(ang, tol_deg=10.0)                 # big error: turn in place first
                self.settle()
            t1 = self.t
            res = {"ok": False, "reason": "timeout"}
            while self.t - t1 < max_s:
                if self.is_fallen() or self.off_table():
                    res = {"ok": False, "reason": "fell" if self.is_fallen() else "off_table"}
                    break
                x, y, yaw, _ = self.pose()
                rel = np.array([x, y]) - c
                remaining, side = -float(rel @ u), float(rel @ n)
                if remaining <= STANDOFF + 0.012:
                    res = {"ok": True}
                    break
                want = ang - math.atan2(side, APPROACH_LEAD)    # aim a little back towards the axis
                err = self._wrap(want - yaw)
                self.twist[:] = [0.4, 0.0, float(np.clip(APPROACH_GAIN * err, -0.8, 0.8))]
                self.tick(5)
            self.twist[:] = 0.0
            self.hold(0.5)
            self.settle()
            if res["ok"]:
                x, y, yaw, _ = self.pose()
                rel = np.array([x, y]) - c
                if abs(float(rel @ n)) > 0.05 or abs(math.degrees(self._wrap(ang - yaw))) > 15.0:
                    res = {"ok": False, "reason": "not_lined_up"}   # ended beside the banana or pointing wrong: try again
                    if attempt < 2:
                        self.back_up(0.12)                      # clear the banana first: a foot swung at it flings it off the table
                else:
                    break
            if res.get("reason") in ("fell", "off_table"):
                break
        res.update(seconds=round(self.t - t0, 1), **self.reach((float(c[0]), float(c[1]))))
        return res

    def back_up(self, dist=0.10, max_s=6.0):
        """Walk BACKWARDS about `dist` m (what is right under the beak is hidden from the camera). Stops early
        before the table edge. Returns dict(ok, moved, seconds)."""
        t0 = self.t
        x0, y0 = self.pose()[:2]
        while self.t - t0 < max_s:
            x, y, _, _ = self.pose()
            if self.is_fallen() or self.off_table() or math.hypot(x - x0, y - y0) >= dist:
                break
            if abs(x) > TABLE_HALF[0] - 0.10 or abs(y) > TABLE_HALF[1] - 0.10:     # too close to the table edge (it still drifts ~4 cm)
                break
            self.twist[:] = [-0.4, 0.0, 0.0]
            self.tick(5)
        self.twist[:] = 0.0
        self.hold(0.5)
        self.settle()
        x, y = self.pose()[:2]
        moved = math.hypot(x - x0, y - y0)
        return {"ok": moved > 0.03 and not self.is_fallen() and not self.off_table(), "moved": round(moved, 3),
                "seconds": round(self.t - t0, 1)}

    def reach(self, target_xy):
        """Where is target_xy relative to the duck? ahead/left in metres and bearing error in degrees.
        The beak lands about 0.095-0.108 m ahead of the trunk, so a good pick has ahead ~0.10, left ~0."""
        x, y, yaw, _ = self.pose()
        dx, dy = target_xy[0] - x, target_xy[1] - y
        c, s = math.cos(-yaw), math.sin(-yaw)
        return {"ahead": round(float(c * dx - s * dy), 3), "left": round(float(s * dx + c * dy), 3),
                "dist_to_target": round(float(math.hypot(dx, dy)), 3),
                "heading_err_deg": round(float(math.degrees(self._wrap(math.atan2(dy, dx) - yaw))), 1)}

    def ground_pick(self, grasp=False, release=False):
        """One crouch-touch-stand cycle (2.8 s). At the deepest point (phase 0.30; the beak is then
        ~0.108 m ahead of the trunk and ~0.03 m above the table) optionally grasp / release.
        Returns dict with what happened."""
        self.twist[:] = 0.0
        self.pick_phase, self.sess = 0.0, self.pick_sess
        info = {"grasped": False, "released": False, "min_dist": None, "angle_err_deg": None,
                "mouth_body": None, "mouth_z_above_table": None}
        did = False
        while self.pick_phase is not None:
            self.tick(1)
            if self.pick_phase is not None and self.pick_phase >= GRASP_PHASE and not did:
                did = True
                mouth = self.data.site_xpos[self.mouth_id].copy()
                info["mouth_z_above_table"] = float(mouth[2] - TABLE_H)
                info["mouth_body"] = self._to_body(mouth).round(3).tolist()
                dist = float(np.linalg.norm(mouth - self.banana_pos()))
                info["min_dist"] = round(dist, 3)
                ang_err = self.grasp_err_deg()                     # the banana must lie across the beak to be held crosswise
                info["angle_err_deg"] = round(ang_err, 1)
                if grasp and not self.holding and dist < GRASP_RADIUS and ang_err <= PICK_ANGLE_TOL:
                    self.holding = True
                    self.model.geom_contype[self.ban_geom] = 0
                    self.model.geom_conaffinity[self.ban_geom] = 0
                    info["grasped"] = True
                elif release and self.holding:
                    self.holding = False
                    d = self.data
                    heading = self.pose()[2]
                    d.qpos[self.ban_q:self.ban_q + 3] = [mouth[0] + RELEASE_FORWARD * math.cos(heading),
                                                         mouth[1] + RELEASE_FORWARD * math.sin(heading),
                                                         TABLE_H + 0.011]    # let it go just in front of the beak tip
                    yaw = heading + HOLD_OFFSET                    # it lands lying across the beak, as it was held
                    d.qpos[self.ban_q + 3:self.ban_q + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
                    d.qvel[self.ban_v:self.ban_v + 6] = 0.0
                    self.model.geom_contype[self.ban_geom] = 1
                    self.model.geom_conaffinity[self.ban_geom] = 1
                    info["released"] = True
        self.hold(0.6)  # let it finish standing / banana settle
        return info

    # ------------------------------------------------------------ observation
    def _to_body(self, p_world):
        """World point -> duck body frame (x forward, y left), relative to trunk."""
        x, y, yaw = self.pose()[:3]
        dx, dy = p_world[0] - x, p_world[1] - y
        c, s = math.cos(-yaw), math.sin(-yaw)
        return np.array([c * dx - s * dy, s * dx + c * dy, p_world[2] - TABLE_H])

    def pose(self):
        """(x, y, yaw, z) of the duck trunk (ground truth - the duck's own odometry)."""
        q = self.free_q
        w, x, y, z = self.data.qpos[q + 3:q + 7]
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return float(self.data.qpos[q]), float(self.data.qpos[q + 1]), yaw, float(self.data.qpos[q + 2])

    def tilt_deg(self):
        g = _rot_inv(self.data.xquat[self.trunk_id], np.array([0.0, 0.0, -1.0]))
        return math.degrees(math.acos(float(np.clip(-g[2], -1, 1))))

    def is_fallen(self):
        x, y, _, z = self.pose()
        return self.tilt_deg() > 60 or z < TABLE_H + 0.06

    def off_table(self):
        x, y, _, _ = self.pose()
        return abs(x) > TABLE_HALF[0] or abs(y) > TABLE_HALF[1]

    def banana_pos(self):
        return self.data.xpos[self.ban_body].copy()

    def banana_axis(self):
        """TRUE direction (rad, world) of the banana's long side. For the judge / the grasp rule - never shown to a duck."""
        ax = self.data.xmat[self.ban_body].reshape(3, 3)[:, 0]
        return math.atan2(float(ax[1]), float(ax[0]))

    def grasp_err_deg(self):
        """How far (0..90 deg) the banana is from lying exactly the way the beak holds it (ACROSS the beak, see
        HOLD_OFFSET). 0 = perfectly across the beak, 90 = lying along the beak. A grasp needs <= PICK_ANGLE_TOL."""
        a = math.degrees(self._wrap(self.banana_axis() - self.pose()[2] - HOLD_OFFSET))
        a = abs(a)
        return min(a, 180.0 - a)

    def box_pos(self):
        return self.data.xpos[self.box_body].copy()

    def banana_in_box(self):
        b, g = self.banana_pos(), self.box_pos()
        return bool(abs(b[0] - g[0]) < PAD_HALF and abs(b[1] - g[1]) < PAD_HALF and b[2] < TABLE_H + 0.03
                    and not self.holding)

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer, self._fast = None, False