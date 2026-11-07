"""scenes.py - ten FIXED scenes for the duck (same layout every time, for every duck and every model) + four PRACTICE scenes (10 to 13).

Table: x from -0.60 to 0.60 m, y from -0.50 to 0.50 m. The duck starts at (-0.40, 0) looking along +x
(x = forward, y = left) unless a scene says otherwise. banana / box = where they lie at the start.
`yaw` is in degrees (0 = looking along +x, 90 = looking to the left). `banana_yaw` is the direction the banana's long side points (default 0 =
along +x, which is the way the duck faces at the start, so in scenes 0-9 the banana has to be approached from the side).

The benchmark (scene_benchmark.py) and pointing_check.py run scenes 0 to N_BENCHMARK-1 by default; the practice scenes 10 to 13 are stepping
stones for learn.py (`--scenes 13 12 10 11 0`): the same layout as scene 0, but the banana is already across the duck's heading (10), turned half way (11),
or across the heading AND the duck starts close, its beak tip 0.24 m from the banana (12), or the second half only: the duck already holds the banana and the
box lies 85 degrees to its right, outside the picture (13, the first rung of the ladder, version 11; scene key `hold=True`, banana/banana_yaw give where the held banana is). They were
added in versions 7, 9 and 11 and do not change scenes 0-9.
A scene may carry `grasp=(radius, degrees, stated)`: the grasp window of that scene. The simulator measures the radius in 3-D from the mouth point (a little above the table and
about 3 cm behind the nominal beak tip while the head is down), so the practice radius of 0.10 m reaches 7 to 10 cm along the table; the duck is told `stated` = 0.07 m, the
smallest reach, and 30 degrees (the benchmark: 5 cm and 20 degrees). Version 10, so that the duck can get as far as carrying and placing; the window is printed in the scene
header and it is in scenes.json. Scenes 0-9 have no `grasp` entry: they always use the benchmark window.
"""

SCENES = [
    dict(name="easy-left",    banana=(0.20, 0.12),  box=(0.20, -0.20), note="like the usual random seeds"),
    dict(name="easy-right",   banana=(0.15, -0.15), box=(0.22, 0.22),  note="mirror image of scene 0"),
    dict(name="beside-box",   banana=(0.20, 0.00),  box=(0.20, 0.18),  note="banana right next to the tray"),
    dict(name="far-ahead",    banana=(0.40, 0.00),  box=(0.30, -0.28), note="long first walk"),
    dict(name="wide-apart",   banana=(0.10, 0.38),  box=(0.10, -0.38), note="far left banana, far right box"),
    dict(name="underfoot",    banana=(-0.36, 0.00), box=(0.20, -0.20), note="HARD: banana only 4 cm in front of the feet"),
    dict(name="banana-behind", banana=(-0.25, 0.22), box=(0.25, -0.15), duck=(0.0, 0.0), yaw=0.0,
         note="duck starts in the middle, banana is behind-left"),
    dict(name="box-behind",   banana=(0.25, 0.15),  box=(-0.20, -0.20), note="the tray is back near the start"),
    dict(name="long-carry",   banana=(0.38, -0.30), box=(-0.05, 0.30), note="carry the banana across the table"),
    dict(name="front-edge",   banana=(0.46, 0.05),  box=(0.20, -0.20), note="banana 14 cm from the table edge"),
    dict(name="across-left",  banana=(0.20, 0.12),  box=(0.20, -0.20), banana_yaw=90.0, grasp=(0.10, 30.0, 0.07), note="PRACTICE: scene 0 with the banana already across the duck's heading"),
    dict(name="slanted-left", banana=(0.20, 0.12),  box=(0.20, -0.20), banana_yaw=45.0, grasp=(0.10, 30.0, 0.07), note="PRACTICE: scene 0 with the banana turned half way (45 degrees)"),
    dict(name="close-start",  banana=(0.20, 0.12),  box=(0.20, -0.20), banana_yaw=90.0, duck=(-0.12, 0.02), yaw=0.0, grasp=(0.10, 30.0, 0.07),
         note="PRACTICE: scene 10, but the duck starts close: its beak tip is 0.24 m from the banana (banana 17 degrees to the left, across its heading); the box is in view"),
    dict(name="carry-start",  banana=(0.19, 0.10),  box=(0.20, -0.20), banana_yaw=105.0, duck=(0.10, 0.07), yaw=15.0, hold=True, grasp=(0.10, 30.0, 0.07),
         note="PRACTICE: the second half of scene 12: the duck already holds the banana (the state after a good grasp in scene 12) and the box is 0.30 m away, "
              "85 degrees to its right, outside the camera's picture"),
]
N_BENCHMARK = 10                # scenes.SCENES[:N_BENCHMARK] are the benchmark; the rest are practice scenes
