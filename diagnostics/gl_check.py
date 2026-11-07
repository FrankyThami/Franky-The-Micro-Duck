"""gl_check.py - how fast is the graphics on this laptop?  (no duck walking, no window, about 20 s)

It asks MuJoCo to draw pictures off-screen and measures how long one picture takes, for a trivial scene and for the duck's
table, with and without shadows / reflections / anti-aliasing. It also prints the name of the OpenGL driver that is
really being used.

    python gl_check.py
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import ctypes
import os
import time

import mujoco

from sim.duck_sim import TABLE_H, Duck

printed = False


def gl_info():
    """Vendor / graphics card / OpenGL version of the context that is current right now."""
    try:
        gl = ctypes.windll.opengl32 if os.name == "nt" else ctypes.CDLL("libGL.so.1")
        gl.glGetString.restype = ctypes.c_char_p
        out = []
        for k in (0x1F00, 0x1F01, 0x1F02):                  # GL_VENDOR, GL_RENDERER, GL_VERSION
            s = gl.glGetString(k)
            out.append(s.decode(errors="replace") if s else "?")
        return out
    except Exception as e:
        return [f"(could not read: {e})"] * 3


def bench(label, model, data, w, h, cam=None, samples=None, shadow=True, refl=True, n=6):
    global printed
    q = model.vis.quality
    saved = q.offsamples
    if samples is not None:
        q.offsamples = samples
    r = mujoco.Renderer(model, height=h, width=w)

    def once():
        r.update_scene(data, camera=cam) if cam is not None else r.update_scene(data)
        r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = int(shadow)
        r.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = int(refl)
        r.render()

    once()
    if not printed:
        printed = True
        vendor, card, version = gl_info()
        print(f"graphics card : {card}\nvendor        : {vendor}\nOpenGL version: {version}\n")
    t0 = time.perf_counter()
    for _ in range(n):
        once()
    ms = 1000 * (time.perf_counter() - t0) / n
    r.close()
    q.offsamples = saved
    print(f"  {label:<62}{ms:8.1f} ms per picture", flush=True)
    return ms


TRIVIAL = ('<mujoco><worldbody><light pos="0 0 3"/><geom type="plane" size="1 1 0.1"/>'
           '<geom type="box" size=".1 .1 .1" pos="0 0 .1" rgba="1 0 0 1"/></worldbody></mujoco>')
tm = mujoco.MjModel.from_xml_string(TRIVIAL)
tm.vis.global_.offwidth, tm.vis.global_.offheight = 1280, 720
td = mujoco.MjData(tm)
mujoco.mj_forward(tm, td)

d = Duck("Franky")                                          # no window
m, data = d.model, d.data
m.vis.global_.offwidth, m.vis.global_.offheight = 1280, 720
cam = mujoco.MjvCamera()
cam.lookat[:] = (0.0, 0.0, TABLE_H)
cam.distance, cam.azimuth, cam.elevation = 1.2, 150, -25

print("--- a trivial scene (one red box on a floor), 640 x 720")
bench("trivial scene", tm, td, 640, 720)
print("\n--- the duck's table, 640 x 720  (the window draws TWO of these per picture)")
a = bench("as in the window: shadows + reflections + 4x anti-aliasing", m, data, 640, 720, cam)
b = bench("no shadows, no reflections", m, data, 640, 720, cam, shadow=False, refl=False)
c = bench("no anti-aliasing", m, data, 640, 720, cam, samples=0)
e = bench("no shadows, no reflections, no anti-aliasing", m, data, 640, 720, cam, samples=0, shadow=False, refl=False)
print(f"\nthe window draws two such pictures per frame: about {2 * a:.0f} ms  ->  {1000 / (2 * a):.1f} frames per second at best")
print(f"the same without shadows / reflections / anti-aliasing: about {2 * e:.0f} ms  ->  {1000 / (2 * e):.1f} frames per second at best")