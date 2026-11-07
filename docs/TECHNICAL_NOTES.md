# MicroDuck Robot - Franky

A MuJoCo simulation of the Microduck robot "Franky" (Pollen Robotics model, BAM servo model, pretrained ONNX walk and
pick policies). **Franky (duck1) is an autonomous LLM agent**: a vision model (Gemma 4, `gemma4:e2b` through Ollama)
gets a command in plain words, looks at the real camera picture, reasons, chooses its own tools and carries the command
out. **It learns:** `duck1/learn.py` lets it try ONE scene again and again, write its own notebook after every attempt, and move on
to the next scene only when the scene is solved (see "Franky learns" below). The second duck (Thami, duck2, `qwen3-vl:2b-instruct`) is a
separate test set and was not touched.

## How Franky works (duck1)

```
 command ("Pick up the banana and put it into the green box.")      <- the ONLY task description
        |
        v
 Gemma 4 (thinking on) --- reads the real camera picture (a JPEG goes to the model) ---> picks ONE tool per turn
        ^                                                                                      |
        |   tool result (raw numbers) + a new camera picture                                    v
        +------------------------- look find measure walk turn close_beak open_beak done --+
```

* **The prompt has no recipe.** `duck1/franky_tools.py` holds everything the model is told: the goal (the command),
  the camera's limits (where it looks, what it cannot see, that a picture is flat), the body's facts (how the beak holds the
  banana: *crosswise*, where the beak tip lands, what can trip it) and the descriptions of the tools. No list of steps.
* **The model really looks.** There is no colour detection that tells it what it sees. It names things (`find`) or points at pixels (`measure`).
* **Tools return raw data, no judgements:**

  | tool | what it does | what comes back |
  |---|---|---|
  | `look` | take a picture | the picture |
  | `find(what)` | the model names a thing ("yellow banana", "blue box"). Gemma is asked, in its own **native detection mode** (JSON boxes, 0-1000), to outline it in the latest picture. One extra Gemma call, no thinking. | for each box: where it is in the picture, where its middle touches the table (metres ahead / left, distance, bearing), the footprint on the table (across x deep, metres); the picture with the boxes drawn |
  | `measure(x,y[,x2,y2])` | the model points at 1 or 2 spots in the picture (0-1000 scale) | for each spot: metres ahead / left, distance, bearing; for two spots also the middle, the length and the angle of the line; the picture with red crosses at the spots |
  | `walk(forward, turn)` | turn on the spot, then walk straight (positive turn = left, negative = right) | what the body REALLY did (odometry: turned X deg, moved Y m) + a new picture |
  | `turn(direction, degrees)` | version 12: turn on the spot, `direction` "left" or "right", `degrees` a positive number (0-180). The same motion as `walk(0, +-degrees)`, with a word for the side instead of a sign | the same as `walk`: what the body REALLY did, the total turned since the start, a new picture |
  | `close_beak` | crouch, close the beak on what is under the beak tip (0.10 m ahead of the body centre), stand up. It does not move the duck and does not aim. | whether the beak closed on the banana; if not, the **beak sense** (below) + a new picture |
  | `open_beak` | crouch, open the beak so that what is held drops at the beak tip | a new picture |
  | `done` | say the command is finished | - |

  Every tool except `done` REQUIRES a `note`: the model's own plan, which stays in its history while old pictures and its thinking
  are dropped (Gemma's thinking is not replayed to it, so without a note it re-derives everything every turn and falls back to a
  fixed routine). Every result also repeats the one fact about the beak that matters: "beak tip: 0.10 m ahead of the centre of your body".
  **Why `close_beak` / `open_beak` and not `pick` / `place`:** in the real runs the 2B model read `pick` as a finished skill ("pick up
  the banana") and called it again and again from 40-60 cm away without ever walking. The names now say what the body does.
  (The old names are still accepted by the code, so older scripts and logs keep working.)
  `find` was added after the first real run (see below): Gemma e2b points badly when it has to write x and y into a function call, but
  drawing boxes is a skill it was trained for. A box is a rough outline (it also covers the object's height, so its depth is overstated)
  and does not say which way a banana points - that is what `measure` on the two ends is for. `--find off` removes the tool.
* **No `walk_to(banana)`.** The model decides how far to go, when to turn, from which side to come at the banana. There is
  no rule brain anywhere in duck1: nothing in this stack can solve a scene without the LLM.
* **The referee** (`agent_harness/referee.py`) reads the TRUE simulator state after every tool call: did the action do what
  it is for, did anything unexpected happen, is the agent looping, and - for every `measure` - how far from the real banana /
  box was the spot the model pointed at? The model never sees any of this.

### Safety and limits (kept)

* **Always on, in `sim/motion.py`** (like a real robot's cliff sensor and bumper; the LLM cannot talk it away):
  a walk or turn that would take the body too close to the table edge, or onto the box rim, is refused; a walk that gets there
  is stopped; the banana right at the feet stops the walk. Limits: forward -0.25 to 0.60 m, turn +-180 degrees.
* **Behaviour guard (`--guard on`, default):** a false `done` is sent back (max 2 times), a repeated failing action gets a
  warning and then ends the episode, budgets of 12 failed moves (walk / close_beak / open_beak / done; since version 6 a failed `find` or `measure` does not count and a grasp starts the count again) / 300 simulated seconds / `--max-steps` tool calls.
  **Stall watch:** if 5 tool calls in a row change nothing in the world (no walk or turn, the banana not moved, the beak holding the same:
  looks, finds, measures, a beak that closed on nothing, refused walks), the model gets one plain warning ("your last 5 tool calls changed
  nothing: you did not move, and your beak took nothing and let nothing go ..."); after 10 the episode ends as `guard_stalled`. It never says what to do instead.
* The grasp rule is unchanged: the banana is held crosswise, picked up only if its middle is within 5 cm of the beak tip and
  it lies across the beak within 20 degrees.
* **Beak sense (version 5, `--beak-sense on|off`, default on; since version 6 also after a close that DID take the banana).** When `close_beak` closes, the result says where the banana's middle
  was relative to the beak tip and how its long side pointed - if it was within 0.10 m of the tip (the strip of table the camera cannot see) -
  otherwise only that it was farther away. Example: `Beak sense: the middle of the banana was 0.02 m behind your beak tip and 0.04 m to the right of it
  (distance 0.05 m); its long side points -20 degrees from the direction you face (0 = along it, +-90 = across it).` Raw numbers in the same
  convention as `measure`, no advice. These numbers come from the simulator, not from the camera: it is the only place where the true state reaches the
  duck during an attempt (the person's verdict after an attempt is the other one). `--beak-sense off` gives the old message ("not under your beak tip,
  or not across your beak") - use it as a control. It costs a crouch each time, and the guards count a failed close like any failed action.
* The picture's grey bars (with `--pad-square on`) are named in the answer: `measure` / `find` on a spot with y below 125 or above 875 now say "that spot
  is in the grey padding, outside what the camera sees" instead of the general "not on the table surface".

## Franky learns: one scene at a time, with a notebook it writes itself  (`duck1/learn.py`)

The model's weights never change. "Learning" is what a person does with a notebook:

```
 attempt  -->  the duck reads the log of what it did and what came back  -->  it writes notes  -->  notebook (a JSON file)
   ^                                                                                                        |
   +------ next attempt (same scene, then the next scene): the notebook is in the duck's first message <-----+
```

1. **Attempt.** The duck gets the command and its notebook and does the scene exactly as in the benchmark (same tools, camera, safety, guards).
2. **Reflect.** When the attempt ends the duck is shown the log of its own attempt (each tool, its arguments, the numbers that came back, its own
   notes, the warnings it got), how the attempt ended, and the person's verdict ("the banana is NOT in the green box. The banana lies on the
   table."). It answers with note lines: `LEARNED:` (a fact about its body, tools or the world), `SKILL:` (a way of doing something, as short
   instructions to itself) and `FORGET:` (a note that this log shows to be wrong; since version 13 with a quote from the log, see below).
3. **Notebook.** `outputs\memory\franky_notebook.json` (and a readable `franky_notebook.md` next to it). Notes: K1, K2 ... (facts), S1, S2 ... (skills).
   Duplicates are merged (`seen` counts how often the duck wrote a note again), at most 10 facts and 6 skills are kept, and for every note the
   file counts the attempts it was shown in (`tries`) and how many of them succeeded (`wins`), so you can see which notes helped.
   **Version 13:** a note is shown in at least 3 attempts before a newer note may push it out (then the note the duck wrote down least often goes first; if no
   note has had its 3 tries the new note is not added), and a `FORGET` must carry a quote: `FORGET: K2 "words copied from the log"`. The code only checks that the
   quote is part of the log of that attempt (the result lines, the warnings, how it ended, the person's verdict; not the duck's own `[note]`s). It does not judge
   whether the quote shows anything. `--forget-quote off` and `--keep-notes old` bring the version-12 behaviour back.
4. **Again.** Next attempt the notebook is in the first message, before the duck looks at anything. When the scene is solved (`--need` successes
   in a row, default 1) the next scene starts with the same notebook: how many attempts does it need there?

**Nothing in the code writes or corrects a note, and it never ranks one by what it says (only by counts: written again, tries, wins).** What is in the notebook is what the model decided to write; a wrong note stays until
the duck forgets it or it is pushed out. The reflection prompt asks for notes that stay useful in a new scene (about the body, the tools and
its own mistakes, not where things were). It does not describe the task.

**What the duck is told about the result:** only the person's verdict after an attempt (banana in the box or not; fell off the table; still in
the beak; "you said done but it was not") - the true state, but only as an outcome, like a person watching. Never positions, never what to do.
`--feedback off` removes even that. This is the one place where the true state reaches the duck; everything it perceives still comes from its camera.

**Controls:** `--learning off` repeats the scene with no notebook at all (does the notebook help, or is it luck?); `--learning frozen` reads the
notebook but never changes it (does what it learned work in a new scene?). Compare "attempts to solve".

## The folders

```
MicroDuck Robot/
|-- project_paths.py      every folder of the project in ONE place (change a location here, nowhere else)
|-- requirements.txt      the python packages
|
|-- sim/                  SHARED simulator pieces
|   |-- duck_sim.py         the duck's body: walking, crouching, the simulated grasp, the table
|   |-- motion.py           Franky's body as ONE command, drive(forward, turn) + the always-on safety layer          (new)
|   |-- head_camera.py      the raw 800x600 head camera: pictures for the model, pixel -> table geometry for `measure`  (new)
|   |-- eye.py              the old camera + colour-blob perception (used by duck2; head_camera reuses its camera mount)
|   `-- recorder.py         saves the MuJoCo footage as an mp4
|
|-- agent_harness/        agent harness
|   |-- gemma_brain.py      the LLM client for Franky: pictures, thinking mode, context management, retries, the `find` detection call  (new)
|   |-- referee.py          the judge on the TRUE state, incl. pointing accuracy                                      (new)
|   |-- agent_loop.py       one episode: conversation, guards, full log (transcript.md, frames/, steps.csv ...)       (new)
|   |-- guardrails.py       the behaviour guard (false-"done" rejection, loop/failure/time budgets, LLM retry)
|   |-- run_harness.py      the run logger (events.jsonl, steps.csv, episodes.csv ...) and helpers
|   |-- franky_agent.py  harness.py  state_tracker.py  memory.py  experiment.py     the OLDER harness (random seeds, memory
|   |                       experiment). duck2 still uses parts of it. Franky (duck1) no longer does.
|
|-- duck1/                TEST SET 1: Franky, one banana + one green box, 10 fixed scenes
|   |-- scenes.py           the 10 scenes
|   |-- franky_tools.py     the prompt, the eight tools                                                                (new)
|   |-- scene_benchmark.py  run all (or some) scenes once each
|   |-- learn.py            ONE scene at a time: attempt, reflect, write notes, try again; then the next scene           (new)
|   |-- franky_memory.py    the notebook: reading the duck's notes, merging, tries / wins, the log the duck reflects on  (new)
|   `-- pointing_check.py   test ONLY the skill everything depends on: can Gemma locate the banana / box in the picture?  (new)
|
|-- duck2/                TEST SET 2 (Thami): blue box + green box, two bananas, behaviour tests N0 - N9   (not changed)
|-- diagnostics/          small checks that are NOT needed to run test set 1 or 2
|-- assets/               third-party files (do not edit)
|-- microduck_rl/         third-party: the official Pollen repo (only its robot model is used)
`-- outputs/              everything the programs write
    |-- logs/duck1/  logs/duck2/  logs/harness_v1/     one folder per run
    |-- videos/duck1/  videos/duck2/                   the mp4 footage + a small .txt that says where each scene starts
    `-- memory/                                        franky_notebook.json + .md (duck1/learn.py); older lessons file (duck2 / harness experiments)
```

## How to run (PowerShell, from the project folder; Ollama must be running with `ollama pull gemma4:e2b` done)

Step 1 - can the model locate things in the picture? (about 36 short model calls, about 2 minutes)

```powershell
python duck1\pointing_check.py                      # the `find` way: Gemma draws boxes, on the normal 4:3 picture AND on a square-padded one
python duck1\pointing_check.py --image-mode native  # only the normal picture
python duck1\pointing_check.py --method point       # the `measure` way (pointing through a tool call)
python duck1\pointing_check.py --model gemma4:e4b   # a bigger model - if e2b cannot locate things, nothing else will help
```
Read the last lines: "median error ... cm" per picture shape and box order, and "best fit: ... picture, boxes read as ..." with the exact flags to
add to the benchmark (`--box-order xyxy`, `--pad-square on`). The offsets (x, y) show whether the model is only shifted in one direction.
A banana middle that is more than ~5 cm off for most scenes means the duck will miss its grabs whatever it plans. Look at the pictures
it saves (`s00_banana_find_native.jpg`, `..._square.jpg`): the red boxes show where Gemma thinks things are.

Step 2 - Franky LEARNS one scene (the main way to run it now):

```powershell
python duck1\learn.py --view                        # scene 0, up to 6 attempts, gemma4:e4b + square picture (the best settings found by pointing_check)
python duck1\learn.py --scenes 0 1 2 3 --view       # a curriculum: moves to scene 1 only when scene 0 is solved, and so on; the notebook goes along
python duck1\learn.py --scenes 13 12 10 11 0 --view   # PRACTICE ladder, then scene 0: 13 = the duck already holds the banana, the box is out of view on its right; 12 = scene 10 but the duck starts close (beak tip 0.24 m from the banana), 10 = banana already across the
                                                    # duck's heading, 11 = banana turned 45 degrees; the duck moves up a rung only when it has solved the one before (--on-fail stop is the default)
python duck1\learn.py --attempts 10 --need 2        # up to 10 attempts; a scene counts as learned after 2 successes in a row
python duck1\learn.py --think off                   # about 3-5x faster per step, but the duck reasons less (the reflection after each attempt still THINKS)
python duck1\learn.py --think off --reflect-think off   # ... and the reflection too does not think (fastest, the notes get shallow)
python duck1\learn.py --learning off                # CONTROL: same repeated attempts, no notebook
python duck1\learn.py --beak-sense off              # CONTROL: the old close_beak message (no numbers about where the banana was)
python duck1\learn.py --fresh                       # empty notebook (the old one is kept as franky_notebook.json.old_<time>); use it after changing the tools (see version 12)
python duck1\learn.py --turn-tool off              # CONTROL: the seven tools of versions 1-11 (no `turn`; walk still turns, positive = left)
python duck1\learn.py --forget-quote off           # CONTROL: the old FORGET (an id is enough, no quote from the log needed)
python duck1\learn.py --keep-notes old             # CONTROL: the old way of making room (the weakest old note goes at once, no 3-attempt trial)
python duck1\learn.py --detail full                # the long terminal output of versions 1-6 (the files on disk are the same either way)
```
You can stop with Ctrl+C at any time: the notebook is saved after every attempt and the learning curve so far is printed. Run it again and the duck
goes on with the same notebook. Read `outputs\memory\franky_notebook.md` to see what it wrote, and `transcript.md` of the run for each attempt, with
the notebook it was given and its reflection. `learning.csv` has one row per attempt (success, tool calls, walks, close_beaks, notes added ...).

Step 3 - Franky on the 10 scenes, one attempt each (the old benchmark, no learning):

```powershell
python duck1\scene_benchmark.py --model gemma4:e4b --pad-square on   # all 10 scenes (the settings pointing_check found best)
python duck1\scene_benchmark.py --view --record          # MuJoCo window + mp4 footage
python duck1\scene_benchmark.py --scenes 0 5 --view      # only some scenes
python duck1\scene_benchmark.py --command "pick this and go and place"   # a loose command: the duck must work out what it means
python duck1\scene_benchmark.py --think off              # no reasoning mode (about 3-5x faster, the duck reasons less)
python duck1\scene_benchmark.py --find off               # no `find` tool: the duck has to point at pixels with `measure` only
python duck1\scene_benchmark.py --turn-tool off          # no `turn` tool (the seven tools of versions 1-11); walk turns in both cases
python duck1\scene_benchmark.py --box-order xyxy         # only if pointing_check says the boxes fit best as xyxy
python duck1\scene_benchmark.py --pad-square on          # only if pointing_check says the square-padded picture fits better
python duck1\scene_benchmark.py --kick-after 3           # the banana is knocked away after the 3rd tool call (the duck is not told)
python duck1\scene_benchmark.py --guard off              # no behaviour guard (the safety layer of the body stays on)
python duck1\scene_benchmark.py --model gemma4:e4b       # another vision model with tool calling
```

Other flags: `--max-steps 40`, `--num-ctx 8192`, `--num-predict 2048`, `--keep-images 1`, `--temperature 0.3`, `--seed 0`,
`--rec-fps 30`, `--logdir`, `--video-dir`.

**The terminal is short by default (version 7):** one line per tool call (`step 7 [ 41s]  walk(forward=0.4, turn=0)  ->  turned 3 deg to the left and moved forward 0.40 m`),
the duck's note (110 characters), `[missed]` when the effect was not what the tool is for, every `## guard:` warning, and per attempt one `beak closes:` line with the numbers
of each close. `--detail full` brings the long output back. The files in `outputs\logs\...` always hold everything.

**Reading a run** (`outputs\logs\duck1\run_<time>_...\`): open `transcript.md` first - for every step what the model THOUGHT, which tool
it called with which note, what came back and what the referee found. `frames\` holds every picture the model was given.
`steps.csv` (one row per tool call) and `episodes.csv` (one row per scene) open in Excel; `events.jsonl` has everything in
order; `run_meta.json` has the exact prompt, tool list, model settings, Ollama version and code hashes.

Test set 2 and the older harness (unchanged; they still contain their own no-LLM test brains):

```powershell
python duck2\scene_benchmark2.py --view --record
python agent_harness\harness.py --seeds 0 1 2 --guard on
python diagnostics\compare_runs.py 6
```

`python -m duck1.scene_benchmark ...` works too. Every script finds the project folder by itself.

## What the scripts need

```powershell
pip install -r requirements.txt
```

The vision model has to fit your GPU: pictures cost context. If `gemma4:e2b` runs slowly or spills to the CPU, lower
`--num-ctx` (e.g. 6144) and keep `--keep-images 1`; `--think off` also helps a lot.

## Honest notes (keep these in any write-up)

* **Still given to the duck for free:** its own position and heading (odometry), the camera's pose when a picture is taken,
  the table height (so a pixel can be turned into metres) and a table-edge / box / banana safety layer. A real robot has
  the first two from its joint angles and leg odometry.
* The model does the perception (finding the banana and the box in the picture) and all the planning. `measure` is
  pure geometry: it does not know what is in the picture.
* The grasp is simulated (the official model has no jaw motor): the banana is attached to the beak tip by code.
* The referee reads the TRUE simulator state; the LLM never sees it. A `done` is judged on the true state, so a duck that
  believes it has finished but dropped the banana on the rim counts as NOT successful.
* **Measured body facts.** In the prompt: the banana is invisible in the strip right under the beak, so the last centimetres of
  a pick have to be planned from odometry. In the `walk` description: a turn smaller than ~30 degrees on the spot springs back, so
  it must be made while walking (needs 0.10 m of forward walk). NOT told, the agent has to notice them in the odometry the tool
  reports: a turn on the spot shuffles the body forward up to ~3 cm (it can push a banana that lies at its feet); after a walk the
  body ends ~7 degrees left of where it was told to face; the box rim disturbs the walk from ~18 cm away, further than the
  12.5 cm keep-out that stops a fall.
* **What the first real run (gemma4:e2b, thinking on, only `look/measure/walk/pick/place/done`) showed:** the model recognised the banana
  and the green box in the picture, but (1) its `measure` spots were far off - median 20 cm for the banana, 16 cm for the box, and the
  banana line always drawn side by side (89 degrees wrong), in all 7 scenes tried; (2) it never called `walk`: in scene 0 it made 14 looks,
  11 picks and 3 measures in 28 calls, picking from 40 cm away (it thought `pick` needs coordinates). Changes made because of it: the `find`
  tool (native boxes), plain facts in the `pick` / `place` descriptions and in the camera / body text ("pick does not move you", "far
  things are small and only roughly pointed at"), and the stall watch. None of it says WHAT to do. Whether it is enough for a 2B model is not known yet.
* **What the second real run (version 2: `find`, stall watch) showed:** `pointing_check` on gemma4:e2b: the boxes are on the right KIND of
  thing but in the wrong place - x about right, y systematically wrong (far things 100-260 units too low, big near things 65-120 too high);
  the box middle was 26 cm off for the banana and 20 cm off for the box (median), 0 of 6 banana boxes within 3 cm, 2 of 10 on the box, and
  no box at all in 2 of 8 banana scenes. In the benchmark the model then did "find, pick, find, find, pick ..." from 60 cm away (it was told
  the banana was ~35 cm ahead, because of the y error), never called `walk`, never wrote a note, and ignored the stall warning. Changes in
  version 3: the beak tools are named `close_beak` / `open_beak`, `note` is required, the prompt says the thinking is not remembered and the
  note is the memory, every result states the beak tip position, and a test of the picture shape (`--pad-square`). Still open, and not fixable
  in code without turning `find` into a hidden vision system: if the real Gemma e2b cannot locate things within a few centimetres in the picture, the duck
  can only grope. `pointing_check.py` tells you which it is before you spend 30 minutes on the scenes; try `--model gemma4:e4b`.
* **What the third real run showed:** `pointing_check --model gemma4:e4b` (4 GB GPU, about 7.5 s per picture): banana boxes 8/8, median 7.5 cm off on
  the normal picture and **5.6 cm on the square picture**; green box median **0.9 cm** (4/10 on it) on the square picture - the square picture fits better
  for both objects and `[y1,x1,y2,x2]` is the right box order. gemma4:e2b (normal picture) was 26.5 / 20.5 cm. So `learn.py` defaults to e4b + `--pad-square on`.
  The benchmark on e2b (normal picture) showed the duck walking toward the banana for the first time (`close_beak` instead of `pick`, notes, the beak-tip
  line), but it still closed the beak from far away, tried `walk(0.3)` three times while "the banana is right at your feet" stopped it, once walked 0.5 m
  past the banana out of sight, and made the same early mistakes in every scene. That is what the notebook is for.
* **What the fourth real run showed (learn.py, gemma4:e4b, square picture, scene 0, 3 full attempts + 1 started):** every attempt failed, the duck
  never took the banana. Replaying its logged poses against the true banana: at all 11 `close_beak` calls the banana lay 4-32 degrees from the duck's
  heading (the grasp needs 70-110), and it was 2-5.5 cm from the beak tip (in the last two closes of attempt 3 about 4 cm to the side, just outside the 5 cm window). The only message was "not under your beak tip,
  or not across your beak", so the duck always assumed the first and crept forward; the walk bumper ("banana right at your feet") then blocked every
  further step, and a turn under 30 degrees with the walk blocked did nothing. The notebook worked mechanically (the duck even cites "K1" in its
  thinking) but its notes were generic ("use find() before closing the beak"), because nothing in the log said why the beak failed. Time: 47 s per tool
  call on average (4 s + 0.136 s per generated token = 7.4 tokens/s on the 4 GB GPU; ~70% of the generated tokens are thinking), 10-22 min per attempt.
  The task is solvable with the same tools: `walk 0.6`, `walk turn 90`, `close_beak` takes the banana in scene 0 (checked in the cloud). Version 5 added the
  beak sense (above), the grey-padding messages, a warning in the `walk` description that a walk under 0.05 m swings the heading by ~10 degrees
  (measured: 0.02 m -> -11 degrees, 0.05 m -> -1, 0.10 m -> +4, 0.20 m -> +6), and a reflection prompt that asks for the *reason* an action failed.
  Not changed (known, small): `find` puts its box about 2.5 % of the picture too low (so a banana 0.60 m away is reported at 0.53 m); the duck sees the
  box drawn on the picture and re-finds as it gets closer.
* **What the fifth real run showed (learn.py --think off, gemma4:e4b, square picture, scene 0, 4 attempts, stopped in the 5th):** attempt 1 failed (14 steps,
  stopped by the loop guard), attempt 2 took the banana ONCE (late in the attempt) and still failed, attempts 3 and 4 never took it. Why attempts 3 and 4 were not
  better than 2: (1) the grasp in attempt 2 was an accident: the duck had turned 52 degrees left towards a banana "48 degrees to the left", which put the
  banana across its beak by chance; every other close that reported an angle, in all four attempts, had the banana 13-34 degrees from the heading, never across. (2) The close
  that took the banana said only "it closed on the banana" - no numbers - so the reflection could not compare it with the failures and wrote a wrong
  reason ("oriented 14 degrees left"), which was then in the notebook of attempts 3 and 4. (3) The duck's two-spot `measure` calls used the left and
  right edge of the `find` box at the same height: that line is horizontal in the picture, so the tool correctly reported "+88 degrees (across)", and
  the duck believed the banana lay across its beak, while `find`'s own footprint line said 0.03 m across by 0.10 m deep (along). (4) The failure budget
  (12 "missed goals") counted failed `find`/`measure` calls (a box in the grey bars, a spot in the padding): 6-7 of the 12 in attempts 2-4, and it ended attempt 2
  right after the grasp. (5) With `--think off` the duck has no private reasoning, only the note in each call, and no plan memory, so every attempt re-derives
  every step: the number of tool calls (14, 31, 36, 28) does not fall until there is a correct note to follow. One accidental grasp in four attempts is noise, not learning.
  **Version 6** changes what the notebook can learn from: the beak sense is reported after a successful close too (a success and a miss can be compared in
  the same numbers); the reflection prompt asks the duck to compare closes and to check its own notes against the log, and the reflection THINKS even with
  `--think off` (`--reflect-think off` to stop that; one call per attempt, a few minutes); the digest the duck reads keeps every close/open/done line and the
  `find` footprints and drops the looking first; the failure budget counts failed moves only and a grasp starts it again; `find` cuts a box at the picture edge instead of
  failing in the padding; the `find` / `measure` descriptions say that a two-spot angle belongs to the duck's own spots. Not changed: the duck still has to
  work out for itself that the banana must be approached from the side; a note is only as good as the one clean success it comes from.
* **What the sixth real run showed (learn.py --think off, version 6, gemma4:e4b, square picture, scene 0, 6 attempts, 0 successes, about 100 minutes):**
  | attempt | calls | ended | beak closes |
  |---|---|---|---|
  | 1 | 25 | loop guard | 5 |
  | 2 | 40 | step cap | 10 |
  | 3 | 40 | step cap | 3 |
  | 4 | 40 | step cap | 5 |
  | 5 | 40 | step cap | 2 |
  | 6 | 37 | failure budget | 6 |

  Attempt 1: the duck walked straight to the banana, stopped with the beak tip 0.06 m from it and closed the beak three times with the banana's long side at -21, -27 and -29
  degrees from its heading (the grasp needs about +-90). The beak sense printed those numbers each time; the duck never turned, called a tool that does not exist (`turn`) and the loop guard
  ended the attempt. Over the whole run: 33 closes, 19 of them from farther than 0.10 m, and every reported angle between -11 and -42 degrees except one (-67). The duck never
  put the beak across the banana, so it never reached carry / place. What made attempts 3-5 worse: the notebook said "walk in small steps (0.05 m)" (skills S1 / S4), so the duck
  crept (mean walk 0.03-0.09 m) and spent 23-24 of 40 calls on find / measure without ever bringing the beak near the banana; the notes were trivia (step numbers read as attempt
  numbers), 16 notes, none with a win; and the prompt was close to the 8192 context limit. The hard part is not the camera or the number: it is that the duck has to see "the
  banana lies along my heading, so I must come at it from the side and turn about 90 degrees", and nothing in the log has ever shown it one grasp to learn that from.
  **Version 7** (no change to what the duck is told about the task): (1) the terminal output is short (above); (2) two PRACTICE scenes, 10 and 11 = scene 0 with the banana across
  the duck's heading (90 degrees) and half way (45 degrees), so the first real grasp, carry and place can happen and be written into the notebook, and then scene 0 is tried with it
  (`--scenes 10 11 0`; version 9 added the first rung, scene 12, below); scenes 0-9 and the benchmark are unchanged (`scenes.N_BENCHMARK = 10`); (3) the reflection prompt asks for notes backed by the log, drops notes that were followed
  without getting closer (FORGET), and forbids step numbers and descriptions of the harness in a note. Checked in the cloud: scene 10 is solved in 5 calls (`walk 0.5 turn 11`,
  `close_beak`, `walk turn -90`, `walk 0.1`, `open_beak`). Not known: whether the real model does it.
* **What the seventh real run showed (learn.py --think off, version 7, scene 10 = scene 0 with the banana already across the duck, attempts 1-5 seen; 0 successes, about 13 min per attempt):**
  for the first time the beak was within 5 cm of the banana, 5 times (attempt 2 steps 9 and 14, attempt 4 steps 16 and 21, attempt 5 step 15), and all 5 missed because the banana's long
  side was 60-68 degrees from the heading (the grasp needs 70-90; checked in the cloud: with the beak exactly on the banana +72 is taken, +70 and +68 are not). Every walk turned the body 2-11
  degrees to the LEFT of what was asked, so after 4-6 walks the heading had drifted 22-30 degrees and the banana, which lay exactly across the start heading, no longer looked across.
  The duck read "+66, not across" but wrote "I need to rotate the banana", and only once turned itself (24 degrees right, the right direction). In attempt 3 it also closed the beak 5 times with the banana
  0.09-0.12 m to its left, reasoning about "ahead" only. The notebook held a wrong rule (S1 "keep the banana within 0.10 m": 0.10 m is the range of the beak sense, the grasp needs 5 cm). The
  only clear carry-over between attempts was a longer first walk (0.03 m in attempt 1, 0.2-0.4 m after). **Version 8:** every `walk` result adds one line, `Total turned since the start: 22 degrees
  to the left` (the body's own heading since the start of the attempt, left minus right; odometry, no advice), and the terminal shows it as `total turned 22 deg left`. Not known: whether the real model
  uses the number to turn back.
  **Version 9 (scene 12 "close-start"):** a practice scene where the duck begins close - its beak tip 0.24 m from the banana (banana 17 degrees to the left, lying across the duck's heading; the
  green box is in the picture) - so that all of an attempt goes into the hard part (the last 10 cm, the heading, then carry and place), which no real run has reached yet. Same layout as scene 10. The ladder
  is `--scenes 12 10 11 0`. Each attempt still starts from the scene's start (nothing is carried over but the notebook). Checked in the cloud: solved by hand in 7 calls
  (`walk 0.2 turn +20`, `walk 0.1 turn -15`, `close_beak`, `walk turn -85`, `walk 0.10`, `open_beak`, `done`); a single arc `walk 0.24 turn +12` ends 19 degrees left of the start and the close misses
  (that is the drift the heading line shows).
  **Version 10 (practice-only grasp window).** In the ninth real run (`--think on`, scene 12) the duck approached well and closed the beak with the banana 0.02 m ahead / 0.01 m left and its long side at +69 degrees (needs 70-90), then could not
  fix the one degree (it confused the banana's bearing with its orientation and turned the wrong way; there is no small on-the-spot turn). Seven replays of sensible moves in the cloud took the banana at the first close 0 times. So the practice scenes
  (10, 11, 12) now have a wider grasp window, set per scene (`grasp=(simulator radius, degrees, stated reach)` in `scenes.py`): simulator radius 10 cm and 30 degrees, and the duck is told "within about 7 cm" and "within about 30 degrees" in its
  own scene (the two numbers in the system prompt and the close_beak description are swapped per scene; scenes 0-9 and `sim/duck_sim.py` are unchanged and keep 5 cm / 20 degrees). The scene header, `transcript.md` and `scenes.json` name the window and
  the reflection is told that the window was wider. **What the grasp rule really is:** the simulator measures a 3-D distance from the mouth point (0.028 m above the table, about 3 cm behind the nominal beak tip while the head is down) at the lowest
  point of the crouch, so along the table (what the beak sense reports) the benchmark's "5 cm" is 4 cm to the left, 3 to the right, 2 ahead and 9 behind the nominal tip; the practice radius of 10 cm reaches 10 / 9 / 7 / 10, which is why 7 cm is
  what the duck is told (the smallest, always true). The benchmark prompt still says "5 cm"; it was left as it is so that results stay comparable, but it overstates the reach ahead and to the sides - say so in any write-up. Checked in the cloud: of six
  sensible first closes in scene 12, 0 take the banana at the benchmark window and 6 at the practice window. A success in practice does not show a success at the tight window, and notes learned in practice may not carry over to scene 0.
  **Tenth real run (version 10, `learn.py --scenes 12 10 11 0 --view --fresh --think off`, scene 12, 6 attempts, no success, about 1 h 50 min):** the wider window did its job: the duck took the banana 7 times in 3 of the
  6 attempts (1, 4, 6), for the first time in this project. All six failed at the green box. After the grasp the box lies about 85-90 degrees to the right of the duck, and the camera sees only about 50 degrees to each side, so the box is
  OUTSIDE the picture (checked in the cloud at three post-grasp positions: the box centre projects far outside the picture; it comes into view after a right turn of about 40 degrees and is near the middle after 60). While holding the banana the duck turned right
  once, by 15 degrees; otherwise it walked on until the table edge refused it (6 refusals) or turned left (+90 in attempt 1). In attempt 6 it found the box (0.10 ahead, 0.19 right), walked straight ahead 0.11 m, opened the beak and said done
  (banana 0.19 m beside the box). Twice it tried to turn right with a tool `turn`, which does not exist, and dropped the turn. It also reused start-frame coordinates after moving. The notebook learned the wrong lesson: K2, K6 and K9 say "the vision model fails to find
  the box / banana" (it was out of the picture: the reflecting duck was given only the log, not the camera facts the acting duck had), K7 and S4 are wrong or harmful, and the correct K4 / K5 / S3 / K13 (7 cm, 30 degrees) only repeat what the prompt already says.
  **Version 11:** (1) the reflection gets the same camera and body facts as the acting duck (`franky_tools.body_facts`); no hint about what to look for; (2) practice scene 13 "carry-start": the duck starts holding the banana (the state after a good grasp in scene 12),
  the box 0.30 m away, 85 degrees to its right, outside the picture; the first message is unchanged, the first `look` says "Beak: holding the banana"; solved by hand in 5 calls (`look`, `walk turn -85`, `walk 0.10`, `open_beak`, `done`), not by opening at once, walking straight, a left turn
  or a 20-degree right turn; the ladder is `--scenes 13 12 10 11 0`; (3) the "Unknown tool" answer ends with "(To turn, give walk a turn number.)". Not known: whether the real model turns right far enough, and whether better notes come out of the reflection.
  **Eleventh real run (version 11, `learn.py --scenes 13 12 10 11 0 --view --fresh --think off`, gemma4:e4b; stopped at scene 12 after 6 attempts by `--on-fail stop`, about 1 h 52):** scene 13 (carry-start) was solved in attempt 4, the first full success in this project.
  Attempts 1 and 2 wrote "turn right 45" but passed `turn=45`, which turns LEFT (positive = left); in attempt 3 a right turn of 42 degrees found the box, but the duck lost it again and dropped the banana off the table; in attempt 4 `walk(0.1, turn=-90)` (turned 111 degrees to the right) found the box and it opened the beak
  over it. The notebook of attempt 4 did not hold that lesson, so one success in four attempts is thin evidence of learning. Scene 12 (close-start) was not solved in 6 attempts: the banana was taken in 4 of them, but the box phase failed every time (turned left toward a box on its right, walked backward, opened the beak
  0.11 m short of the box, never found the box). At least four walks passed a positive number for a right turn. The notebook still blames the vision model for things that were outside the picture, and "after the grasp turn right 60-90 degrees" never got into it.
  **Version 12 (the `turn(direction, degrees)` tool; the owner's idea, the sign errors are the reason):** `turn` is `walk(forward=0, turn=+-degrees)` with the side given as the word "left" or "right" and a positive number, so a sign cannot be wrong. It returns what the body really did,
  the total turned since the start and a new picture, exactly like `walk`; a turn under about 30 degrees is still poor (the description says so and points to `walk` with 0.10 m forward). It is the same in every scene, benchmark included (the tool list is part of the
  prompt, so the benchmark's tool list changes from 7 to 8 tools; `--turn-tool off` gives the old tool set on both `scene_benchmark.py` and `learn.py`, and then a call of `turn` is answered "Unknown tool ... (To turn, give walk a turn number.)"). The description gives no advice on WHEN to turn
  (no "if you cannot see the box, turn"). The referee, the digest and the terminal treat `turn` as `walk`; `learning.csv` has a new `turns` column. **Not tested with the real model.** Checked in the cloud: `turn right 60` turns the body about 57 degrees to the right, scene 13 is solved by hand
  with `turn right 85, walk 0.10, open_beak, done` and not with `turn left 85`, bad arguments are refused in words, and the benchmark scenes still give 9/10 (only scene 0 missing) for the scripted stand-in, which uses `walk` only. **Use `--fresh`:** the old notebook holds a note such as "there is no dedicated turn tool", which is now false.
  Success on the practice rungs (wide grasp window) does not show success at the tight benchmark window.
  **Twelfth real run (version 12, `learn.py --scenes 13 12 10 11 0 --view --fresh --think off`, about 3 h):** scene 13 learned at attempt 4 (1 of 4), scene 12 learned at attempt 3 (1 of 3: the first complete pick-and-place; the winning attempt grasped, then `turn(right, 90)`, found the box,
  turned right 10 and 5 degrees and opened the beak over it), scene 10 not solved in 6 attempts (the beak took the banana once, in attempt 6, then the box was never found). 36 `turn` calls, 20 left and 16 right.
  **Control run (same code, `--learning off`, scenes 13 and 12, 6 attempts each):** scene 13 0 of 6, scene 12 1 of 6. So with the notebook 2 successes in 7 attempts, without it 1 in 12: it points the same way as "the notebook helps a little" but 7 and 12 attempts are too few to show it (one-sided Fisher p = 0.30).
  Without the notebook 5 of 12 attempts ended because the model sent an empty reply (none of the 7 with the notebook), and the duck twice declared the command done while the banana was not in the box. In scene 10 the notebook did not hold on to what it learned (below).
  **Version 13 (notes that stay long enough to be tested):** the notebook history of the twelfth run shows 5 notes deleted by `FORGET` but about 28 pushed out to make room (17 of the 33 notes that left were gone after being shown ONCE). The old rule gave a note permanent protection for being in the notebook during a success (`wins x 3`), so the first notes
  (scene 13) stayed for ever and every newer note was the weakest. Version 13 changes two things, each with a switch: (1) `--keep-notes fair` (default): a note is only pushed out after it was shown in 3 attempts, the one written down again least often goes first (wins only break a tie), and when no note has had its 3 tries the new note is not added;
  (2) `--forget-quote on` (default): a `FORGET` must carry a quote from the log, which the code checks is really in the log. Replaying the duck's 13 real reflections through the old rule reproduces the real evictions exactly (13 of 13); through the new rule 1 of 34 departing notes was gone after one attempt (median stay 3 attempts instead of 1).
  The number of notes pushed out hardly changes (28 before, 29 after): the duck writes 4-5 new notes per attempt and the notebook has 16 places, so notes still stay only about 3 attempts. A stay of 5 attempts (`MIN_TRIES = 5` in `franky_memory.py`) would refuse 7 new notes in the same replay. **Not known:** whether e4b writes exact quotes (if it does not, forgetting
  becomes rare, which is the safe side), and whether the notes that now stay are any more useful: the grasp rule, for instance, is already in the prompt, so keeping a note that repeats it adds nothing. The quote check cannot catch a quote that is real but misread (the duck quoting `TAKEN ... long side +72 deg` as proof that a note about the grasp window is wrong).
* **Learning, honestly:** whether a 2-4B model writes useful notes is not known until you run it; a note can be wrong, and `tries` / `wins` in the
  notebook show it. The notebook cannot make the perception better: if `find` is 20 cm off, the notes can only teach the duck to cope with it. A
  scene is "learned" when the banana really ends up in the box (judged on the true state); the duck is only told the outcome after the attempt.
  The `--learning off` control tells you how much of any improvement is the notebook and how much is the sampling luck of repeated attempts.
* **In all 10 start scenes the banana lies along the direction the duck faces at the start** (so the beak, which needs it across, has to
  come at it from the side). In the picture a banana far out to the left or right looks slanted, but it is the same everywhere. Working
  this out is the part a 2B model will find hardest.
* **Checked in the cloud, without the real model:** the whole stack (tools, camera geometry, motion, referee, loop, logging,
  CLI, Ollama protocol with a fake server) was run end to end, and the scenes were shown to be solvable with exactly
  these seven tools (`walk` turns; the stand-in does not use `turn`) by a throw-away scripted stand-in (not part of this folder) that points at noisy pixels. It solved 9/10
  with pointing noise of 0.3 % of the picture width, 7/10 with 0.8 % and 6/10 with 1.5 %. **The real Gemma 4 run - how well it
  points, how long it takes on your GPU, whether thinking + pictures + tools work together in your Ollama - has not been
  run.** Expect real results to be lower than the stand-in's. Run `pointing_check.py` first.
