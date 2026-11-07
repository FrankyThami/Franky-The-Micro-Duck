# MicroDuck Robot - Franky

A MuJoCo simulation of the Microduck robot "Franky" (Pollen Robotics model, BAM servo model, pretrained ONNX walk and
pick policies). An LLM agent (gemma4:e2b through Ollama; the second duck is qwen3-vl:2b-instruct, "Thami") has to pick a
banana and put it in the green box. It sees only through its own head camera, and its first move is its own `observe`.

## The folders

```
MicroDuck Robot/
|-- project_paths.py      every folder of the project in ONE place (change a location here, nowhere else)
|-- requirements.txt      the python packages
|
|-- sim/                  SHARED simulator pieces (used by both test sets)
|   |-- duck_sim.py         the duck's body: walking, crouching, the simulated grasp, the table
|   |-- eye.py              the head camera + colour-blob perception
|   `-- recorder.py         saves the MuJoCo footage as an mp4
|
|-- agent_harness/        SHARED agent harness (runs an LLM agent on a duck)
|   |-- franky_agent.py     the brains: OllamaBrain (the LLM) and RuleBrain (no LLM), first tool set + prompt
|   |-- harness.py          the agent loop (one episode), with the tracker and the guardrails
|   |-- state_tracker.py    SEES: after every tool call it checks the TRUE state (did it work? unexpected? looping?)
|   |-- guardrails.py       ACTS: table-edge refusal, false-"done" rejection, loop/failure/time budgets, LLM retry
|   |-- run_harness.py      the run logger (events.jsonl, steps.csv, episodes.csv ...) and helpers
|   |-- memory.py           Franky's notebook of lessons
|   `-- experiment.py       the memory experiment (train / test) and the logger the benchmarks reuse
|
|-- duck1/                TEST SET 1: one banana + one green box, 10 fixed scenes
|   |-- scenes.py           the 10 scenes
|   |-- eye_tools.py        Franky's camera-only tools, prompt and referee
|   `-- scene_benchmark.py  run it
|
|-- duck2/                TEST SET 2: blue box + green box, two bananas, behaviour tests N0 - N9
|   |-- duck2.py            the body with a blue box and a second banana (a subclass of sim/duck_sim.py)
|   |-- eye2.py             the camera that sees several bananas and both boxes
|   |-- tools2.py           tools, prompt, referee (judge) and a no-LLM test brain
|   |-- scenes2.py          the 10 new scenes
|   `-- scene_benchmark2.py run it
|
|-- diagnostics/          small checks that are NOT needed to run test set 1 or 2
|   |-- body_test.py  camera_test.py  eye_test.py     body skills / camera pictures / does the eye find things
|   |-- gl_check.py  smooth_check.py                  graphics speed / smoothness of the window
|   |-- llm_speed_test.py                             which Ollama model is fast enough
|   |-- scripted_baseline.py                          the whole task with NO LLM and true positions
|   `-- compare_runs.py                               one table for the newest runs in outputs/logs
|
|-- assets/               third-party files (do not edit)
|   `-- policies/           Pollen's pretrained ONNX policies
|-- microduck_rl/         third-party: the official Pollen repo (only its robot model is used; it is a git repo)
|
`-- outputs/              everything the programs write
    |-- logs/duck1/  logs/duck2/  logs/harness_v1/     one folder per run: run_<date>_<model>_.../
    |-- videos/duck1/  videos/duck2/                   the mp4 footage + a small .txt that says where each scene starts
    |-- memory/                                        Franky's notebook (lessons_*.json)
    `-- camera_test/                                   pictures from diagnostics/camera_test.py
```

`microduck_rl/` and `assets/policies/` can sit either in `assets/` or directly next to this file: `project_paths.py`
uses whichever it finds.

## How to run (PowerShell, from the project folder)

Test set 1 (10 scenes, one banana):

```powershell
python duck1\scene_benchmark.py --brain rules               # NO LLM: a rule-follower, checks that everything works
python duck1\scene_benchmark.py --view --record             # Franky (gemma4:e2b), MuJoCo window + mp4 footage
python duck1\scene_benchmark.py --model qwen3-vl:2b-instruct   # the second duck (Thami)
python duck1\scene_benchmark.py --scenes 5 6 --view         # only some scenes
```

Test set 2 (N0 - N9, blue box, two bananas ...):

```powershell
python duck2\scene_benchmark2.py --brain rules              # NO LLM (expect: solved 10/10)
python duck2\scene_benchmark2.py --view --record            # Franky (gemma4:e2b)
python duck2\scene_benchmark2.py --scenes 4 5 --view        # only N4 and N5
python duck2\scene_benchmark2.py --perception oracle        # "perfect eyes", for comparison
```

Older harness experiments (random banana/box layouts from a seed):

```powershell
python agent_harness\harness.py --brain rules --seeds 0 26
python agent_harness\harness.py --seeds 0 1 2 --guard on
python agent_harness\experiment.py --brain rules --train 0 1 --test 2 3 --kick-after 1
```

Diagnostics:

```powershell
python diagnostics\compare_runs.py 6            # newest 6 runs, all tests
python diagnostics\compare_runs.py 6 outputs\logs\duck2
python diagnostics\gl_check.py
python diagnostics\smooth_check.py
```

`python -m duck2.scene_benchmark2 ...` works too. Every script finds the project folder by itself, so it does not matter
which folder the terminal is in.

## What the scripts need

```powershell
pip install -r requirements.txt
```

Ollama must be running for the LLM runs (`--brain rules` needs no Ollama).

## Honest notes (keep these in any write-up)

* The camera perception is hand-written colour-blob detection plus table-plane geometry; the duck gets its own position
  and heading (odometry), the table height and the banana's size for free.
* The grasp is simulated (the official model has no jaw motor): the banana is attached to the beak tip by code.
* The referee reads the TRUE simulator state; the LLM never sees it.
* The rule brain (`--brain rules`) is a recipe-follower, not a test of the LLM.
