"""camera_test.py - STEP 1b: what does Franky's own eye see (with the corrected camera)?

For each of the 10 scenes it puts the duck at its start position, takes ONE picture from the head camera,
saves it in the folder  camera_test/  and prints what the camera can see.
It changes nothing in your other files and does not use the LLM.

    python camera_test.py
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import math
import os
import struct
import zlib

import mujoco
import numpy as np

from duck1 import scenes
from sim.duck_sim import Duck, TABLE_H
from project_paths import CAMERA_DIR
from sim.eye import CAM, H, W, fix_eye

OUT = CAMERA_DIR          # pictures go here (outputs/camera_test)
MIN_PIXELS = 8             # fewer coloured pixels than this = "not seen"


def save_png(path, rgb):
    """Write an RGB picture (H x W x 3, uint8) as a PNG file. No extra library needed."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def colour_masks(img):
    """First, simple guess: banana = yellow (red ~ green, little blue), box = green. Step 2 will improve this."""
    r, g, b = (img[..., i].astype(np.int32) for i in range(3))
    yellow = (r > 80) & (b * 10 < r * 3) & (g * 100 > r * 78)
    green = (g > 60) & (g * 10 > r * 14) & (g * 10 > b * 13)
    return yellow, green


def table_point(pos, R, f, u, v):
    """Where does the ray through picture pixel (u, v) hit the table top? None = it does not (sky / far away)."""
    ray = R @ np.array([(u - W / 2) / f, -(v - H / 2) / f, -1.0])     # camera looks along its -z axis
    if ray[2] >= -1e-6:
        return None
    return pos + ((TABLE_H - pos[2]) / ray[2]) * ray


def describe_camera(duck):
    """Where is the camera, where does it look, and which part of the table does it cover?"""
    m, d = duck.model, duck.data
    cid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, CAM)
    pos, R = d.cam_xpos[cid].copy(), d.cam_xmat[cid].reshape(3, 3)
    look = -R[:, 2]
    fovy = float(m.cam_fovy[cid])
    f = (H / 2) / math.tan(math.radians(fovy) / 2)
    fovx = math.degrees(2 * math.atan(math.tan(math.radians(fovy) / 2) * W / H))
    yaw = duck.pose()[2]
    rel = math.degrees((math.atan2(look[1], look[0]) - yaw + math.pi) % (2 * math.pi) - math.pi)
    print(f"  camera '{CAM}': {pos[2] - TABLE_H:.3f} m above the table, looks {math.degrees(math.asin(-look[2])):.1f} deg "
          f"DOWN from horizontal and {rel:+.1f} deg sideways from the duck's heading")
    print(f"  field of view: {fovx:.0f} deg wide x {fovy:.0f} deg high")
    for name, v in (("bottom of the picture", H - 1), ("middle of the picture", H // 2), ("top of the picture", 0)):
        p = table_point(pos, R, f, W / 2, v)
        if p is None:
            print(f"  {name:<22}: no table there (sky / beyond the table)")
        else:
            a, l, _ = duck._to_body(p)
            print(f"  {name:<22}: sees the table {a:+.2f} m ahead, {l:+.2f} m left of the duck's trunk")
    print("  (the beak lands about 0.10 m ahead of the trunk)")


def main():
    os.makedirs(OUT, exist_ok=True)
    duck = Duck("Franky", viewer=False, realtime=False)
    fix_eye(duck.model)                                   # <- the camera in the model faces backwards: re-mount it
    try:
        cam = mujoco.Renderer(duck.model, height=H, width=W)
    except Exception as e:
        raise SystemExit(f"Could not open the offscreen renderer: {e}\nPlease send me this message.")

    print(f"Pictures are saved in the folder '{OUT}' ({W} x {H} pixels).\n")
    for sid, sc in enumerate(scenes.SCENES):
        duck.reset(seed=sid, duck_xy=sc.get("duck", (-0.40, 0.0)), duck_yaw=math.radians(sc.get("yaw", 0.0)),
                   banana_xy=sc["banana"], box_xy=sc["box"])
        mujoco.mj_forward(duck.model, duck.data)
        cam.update_scene(duck.data, camera=CAM)
        img = cam.render().copy()
        path = os.path.join(OUT, f"scene_{sid}_{sc['name']}.png")
        save_png(path, img)
        yellow, green = colour_masks(img)
        ny, ng = int(yellow.sum()), int(green.sum())
        print(f"scene {sid} {sc['name']:<14} banana (yellow) pixels: {ny:>5} -> {'SEEN' if ny >= MIN_PIXELS else 'NOT seen'}"
              f"   box (green) pixels: {ng:>5} -> {'SEEN' if ng >= MIN_PIXELS else 'NOT seen'}")
        if sid == 0:
            describe_camera(duck)
    cam.close()
    duck.close()
    print(f"\nDone. Open the folder '{OUT}' and look at the pictures.")


if __name__ == "__main__":
    main()