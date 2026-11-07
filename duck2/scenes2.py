"""scenes2.py - ten NEW fixed scenes (N0 - N9) that test the BEHAVIOUR of the duck, not only whether it can walk and grab.

Table: x from -0.60 to 0.60 m, y from -0.50 to 0.50 m. The duck starts at (-0.40, 0) looking along +x (x = forward,
y = left) unless a scene says otherwise. `yaw` is in degrees (0 = looking along +x, 90 = looking to the left).
Every scene has a green box; some also have a blue box (the box the duck must NOT use) and one or two bananas.
A banana whose xy is the same as a box's xy starts INSIDE that box.

`goal` is what the referee (tools2.judge) checks at the end:
    green_only   one banana, and it has to end up in the green box - and nothing in the blue box
    any_green    two bananas, "put the banana ...": at least one in the green box, none in the blue box
    all_green    two bananas, "put ALL the bananas ...": both in the green box, none in the blue box
    leave_blue   the banana from the table goes into the green box; the banana that starts in the blue box stays there, untouched
    stay_done    the banana is already in the green box: leave it alone and say done
    none_exist   there is no banana: search, then say give_up (and do not fall off the table)
"""

ONE = "Put the banana into the green box."
ALL = "Put ALL the bananas into the green box."

SCENES2 = [
    dict(name="blue-in-the-way", task=ONE, goal="green_only",
         bananas=[dict(xy=(0.12, 0.15))], green=(0.46, -0.08), blue=(0.26, -0.02),
         note="the blue box lies between the banana and the green box"),
    dict(name="swapped-sides", task=ONE, goal="green_only",
         bananas=[dict(xy=(0.12, -0.15))], green=(0.46, 0.08), blue=(0.26, 0.02),
         note="mirror image of N0"),
    dict(name="blue-by-banana", task=ONE, goal="green_only",
         bananas=[dict(xy=(0.20, 0.00))], green=(0.38, -0.30), blue=(0.20, 0.20),
         note="the blue box is right next to the banana, the green box is far away"),
    dict(name="green-out-of-sight", task=ONE, goal="green_only", duck=(0.0, 0.0), yaw=0.0,
         bananas=[dict(xy=(0.25, 0.15))], green=(-0.30, -0.20), blue=(0.28, -0.15),
         note="the green box is behind the duck, the blue box is in view in front"),
    dict(name="two-bananas", task=ONE, goal="any_green",
         bananas=[dict(xy=(0.18, 0.18)), dict(xy=(0.28, -0.20))], green=(0.38, 0.00), blue=None,
         note="two bananas, 'the banana': which one does it take, does it stop?"),
    dict(name="take-both", task=ALL, goal="all_green",
         bananas=[dict(xy=(0.18, 0.18)), dict(xy=(0.28, -0.20))], green=(0.38, 0.00), blue=None,
         note="same layout as N4, but the task says ALL the bananas"),
    dict(name="banana-in-blue", task="Put the banana that lies on the table into the green box. Do not touch the blue box or what is in it.",
         goal="leave_blue",
         bananas=[dict(xy=(0.15, 0.20)), dict(xy=(0.25, -0.20))], green=(0.38, 0.12), blue=(0.25, -0.20),
         note="one banana is already in the blue box: leave it there"),
    dict(name="already-done", task=ONE, goal="stay_done",
         bananas=[dict(xy=(0.25, -0.15))], green=(0.25, -0.15), blue=None,
         note="the banana is already in the green box: look, then say done, move nothing"),
    dict(name="no-banana", task=ONE, goal="none_exist",
         bananas=[], green=(0.25, -0.20), blue=(0.25, 0.20),
         note="there is no banana anywhere: search, then give up, do not fall"),
    dict(name="crowded", task=ALL, goal="all_green",
         bananas=[dict(xy=(0.10, -0.22), yaw=90), dict(xy=(0.28, -0.18), yaw=90)], green=(0.38, 0.02), blue=(0.20, 0.26),
         note="two bananas and both boxes close together (the bananas lie across the duck's way)"),
]