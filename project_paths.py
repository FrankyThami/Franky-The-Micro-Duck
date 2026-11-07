"""project_paths.py - every folder of the project in ONE place.

Nothing else in the project builds a path from "the folder this file is in" any more: the code asks this module.
So you can start any script from any folder, and when something moves you change it here (and only here).

    ROOT         the project folder (where this file is)
    POLICY_DIR   Pollen's pretrained ONNX policies                  (assets/policies, or policies/ next to this file)
    ROBOT_DIR    the official Microduck MuJoCo model                (assets/microduck_rl/..., or microduck_rl/... next to this file)
    LOG_DIR      outputs/logs/<duck1 | duck2 | harness_v1>/run_<date>_<model>_.../
    VIDEO_DIR    outputs/videos/<duck1 | duck2>/
    MEMORY_DIR   outputs/memory/        (Franky's notebook of lessons, used by agent_harness/experiment.py)
    CAMERA_DIR   outputs/camera_test/   (pictures from diagnostics/camera_test.py)
"""

import os

ROOT = os.path.dirname(os.path.abspath(__file__))

ASSETS = os.path.join(ROOT, "assets")
OUTPUTS = os.path.join(ROOT, "outputs")
LOG_DIR = os.path.join(OUTPUTS, "logs")
VIDEO_DIR = os.path.join(OUTPUTS, "videos")
MEMORY_DIR = os.path.join(OUTPUTS, "memory")
CAMERA_DIR = os.path.join(OUTPUTS, "camera_test")

# the folders that hold the project's own python code (file_hash() looks for a file name in these)
CODE_DIRS = [os.path.join(ROOT, d) for d in ("sim", "agent_harness", "duck1", "duck2", "diagnostics")]


def _first_existing(*candidates):
    """The first folder that exists (so the third-party folders may sit in assets/ or directly in the project folder)."""
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


POLICY_DIR = _first_existing(os.path.join(ASSETS, "policies"), os.path.join(ROOT, "policies"))
_ROBOT_REL = ("src", "mjlab_microduck", "robot", "microduck")
ROBOT_DIR = _first_existing(os.path.join(ASSETS, "microduck_rl", *_ROBOT_REL), os.path.join(ROOT, "microduck_rl", *_ROBOT_REL))


def log_dir(name):
    """outputs/logs/<name> - name is 'duck1', 'duck2' or 'harness_v1'."""
    return os.path.join(LOG_DIR, name)


def video_dir(name):
    """outputs/videos/<name> - name is 'duck1' or 'duck2'."""
    return os.path.join(VIDEO_DIR, name)


def find_code(filename):
    """Full path of a project .py file by its name (e.g. 'duck_sim.py'), or None. Used to fingerprint the code in the logs."""
    for d in CODE_DIRS:
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    return None
