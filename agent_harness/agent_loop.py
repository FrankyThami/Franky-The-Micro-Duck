"""agent_loop.py - one episode of an AUTONOMOUS agent: the LLM gets a command, looks, thinks, and calls tools until it says done.

Nothing in this loop knows about bananas. It only does what a harness should do:
  * builds the conversation: system prompt, the person's command, then (tool call, tool result, camera picture) for every step
  * sends it to the brain (GemmaBrain) and executes the ONE tool the model chose
  * sends the camera picture of every result as a real image
  * the REFEREE (referee.py) judges every step on the TRUE state: it only writes the log, the LLM never sees it
  * the GUARDRAILS act in plain code: the table-edge / box-rim safety is in sim/motion.py; here are the false-"done" rejection,
    the loop warning / stop, the failure budget and the time budget (all switched by guard_on)
  * a complete log: events.jsonl, steps.csv, episodes.csv, transcript.md (what the model THOUGHT and did), frames/ (every picture it saw)

The guardrails never tell the LLM what to do next, they only say that something is wrong ("do not repeat it", "not finished",
"your last 5 calls changed nothing").
"""

import base64
import json
import math
import os
import re
import time
import traceback

from agent_harness.guardrails import Guard, LLMError, chat_with_retry
from agent_harness.harness import kick_banana
from agent_harness.run_harness import RunLogger, truth
from sim.duck_sim import ViewerClosed

MAX_DONE_REJECTS = 2          # a false "done" is sent back at most this many times per episode
MAX_FAILED_GOALS = 12         # stop the episode after this many failed MOVES (walk / close_beak / open_beak / done) since the last grasp.
                              # (Version 5 counted failed looking too - find with no box, measure of the grey padding - and never started again:
                              # in the 5th real run 6-7 of the 12 were such steps and three attempts were cut short, one right after a grasp.)
MAX_SIM_S = 300.0             # stop the episode after this much SIMULATED time (low-level walking needs far more than walk_to did)
CTX_WARN = 0.85
STALL_WARN = 5                # this many tool calls in a row that changed NOTHING in the world -> a warning to the model ...
STALL_STOP = 10               # ... and the episode is stopped if it goes on to this many (the world = body pose, the banana, what the beak holds)
STALL_MOVE_M, STALL_TURN_DEG = 0.02, 5.0      # a move smaller than this is not a change

POSITION_KEYS = ("banana_in_box", "banana_x", "banana_y", "banana_z", "duck_x", "duck_y", "duck_yaw_deg", "tilt_deg", "fallen",
                 "off_table", "box_x", "box_y")


# =============================================================================== the logger
class AgentLogger(RunLogger):
    STEP_COLS = ["run_id", "episode", "scene", "step", "tool", "args", "effect_ok", "expected", "actual", "loop", "unexpected",
                 "guard", "pointing", "result", "think_chars", "llm_s", "prompt_tokens", "gen_tokens", "sim_s", "t_sim", "t_run",
                 "holding_after", "frame"] + list(POSITION_KEYS)
    EP_COLS = ["run_id", "episode", "scene", "model", "command", "success", "claimed_done", "false_claim", "ended", "steps",
               "first_tool", "tool_counts", "failed_effects", "loop_flags", "unexpected_events", "refusals", "stops",
               "measures", "measures_on_object", "finds", "guard_actions", "done_rejected", "no_tool_calls", "llm_s", "prompt_tokens",
               "gen_tokens", "sim_s", "wall_s", "final_dist_banana_to_box"]

    def __init__(self, root, label):
        super().__init__(root, label)
        self.frames_dir = os.path.join(self.dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self._tr = open(os.path.join(self.dir, "transcript.md"), "w", encoding="utf-8")

    def transcript(self, text=""):
        self._tr.write(text + "\n")
        self._tr.flush()

    def save_frame(self, name, jpeg):
        path = os.path.join(self.frames_dir, name)
        with open(path, "wb") as f:
            f.write(jpeg)
        return os.path.join("frames", name)

    def close(self):
        self._tr.close()
        super().close()


def _loggable(messages):
    """Messages without the base64 pictures (they are saved as files)."""
    out = []
    for m in messages:
        c = dict(m)
        if c.get("images"):
            c["images"] = f"<{len(c['images'])} picture(s), {sum(len(i) for i in c['images']) * 3 // 4 // 1024} KB>"
        out.append(c)
    return out


class StallWatch:
    """Counts the tool calls in a row after which nothing in the world was different (the body has not moved or turned, the banana has
    not moved, the beak holds the same). look / find / measure / a beak that closed on nothing / a refused walk all count as "nothing changed".
    After STALL_WARN of them the model gets one plain warning; after STALL_STOP the episode is stopped. It never says what to do."""

    def __init__(self, on=True):
        self.on = on
        self.start(None)

    def start(self, state):
        self.ref, self.n, self.warned = state, 0, False

    @staticmethod
    def _changed(a, b):
        dyaw = abs((b["duck_yaw_deg"] - a["duck_yaw_deg"] + 180.0) % 360.0 - 180.0)
        return (math.hypot(b["duck_x"] - a["duck_x"], b["duck_y"] - a["duck_y"]) > STALL_MOVE_M or dyaw > STALL_TURN_DEG
                or a["holding"] != b["holding"] or math.hypot(b["banana_x"] - a["banana_x"], b["banana_y"] - a["banana_y"]) > 0.01)

    def after_call(self, state):
        """-> (None, None) | ("warn", text) | ("abort", "guard_stalled")"""
        if not self.on or self.ref is None:
            return None, None
        if self._changed(self.ref, state):
            self.ref, self.n, self.warned = state, 0, False
            return None, None
        self.n += 1
        if self.n >= STALL_STOP:
            return "abort", "guard_stalled"
        if self.n >= STALL_WARN and not self.warned:
            self.warned = True
            return "warn", (f"WARNING: your last {self.n} tool calls changed nothing: you did not move, and your beak took nothing and "
                            f"let nothing go. Doing the same again will give the same result.")
        return None, None


def brief(name, text):
    """A tool result in one short line for the terminal (the full text is in transcript.md and steps.csv). Numbers only, as in the result."""
    t = (text or "").strip()
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    first = lines[0] if lines else ""
    sub = lambda s: s.replace(" degrees", " deg")
    if name == "look":
        return "picture taken"
    if name in ("close_beak", "pick"):
        sense = next((l for l in lines if l.startswith("Beak sense:")), "")
        if "closed on the banana" in first:
            verdict = "TAKEN the banana"
        elif first.startswith("You crouched"):
            verdict = "NOT taken"
        else:
            return sub(first)[:110]
        m = re.search(r"was ([\d.]+) m (ahead of|behind) your beak tip and ([\d.]+) m to the (left|right) of it .*?points ([+-]\d+) degrees", sense)
        if m:
            return f"{verdict}; banana {m.group(1)} m {'ahead' if m.group(2) == 'ahead of' else 'behind'}, {m.group(3)} m {m.group(4)} of the beak tip, long side {m.group(5)} deg"
        if "more than" in sense:
            return f"{verdict}; banana farther than 0.10 m from the beak tip"
        return verdict
    if name in ("walk", "turn"):
        h = re.search(r"\s*Total turned since the start: (\d+) degrees(?: to the (left|right))?\.", first)
        first = first.replace(h.group(0), "") if h else first
        hs = ("; total turned " + (f"{h.group(1)} deg {h.group(2)}" if h.group(2) else "0 deg")) if h else ""
        f = re.sub(r"\s*\(you asked:[^)]*\)\.?", lambda m: hs, first)           # the heading goes where the "you asked" part was: right after the odometry
        f = re.sub(r"\.? It stopped early", "; stopped early", f)
        f = f.replace("Your body ", "").replace("Refused for safety: ", "REFUSED: ")
        return sub(f).rstrip(".")[:150]
    if name == "find":
        if not first.startswith("Found"):
            return "no box" if "no box" in first else sub(first)[:90]
        out = []
        for m in re.finditer(r'box \d+ \("([^"]*)"\):.*?(?=\nbox \d+ \(|\nThe picture below|\Z)', t, re.S):
            body = m.group(0)
            mid = re.search(r"middle of the box on the table: ([\d.]+) m ahead, ([\d.]+) m to the (left|right)", body)
            piece = f"{m.group(1) or 'box'}: " + (f"{mid.group(1)} ahead, {mid.group(2)} {mid.group(3)}" if mid else "middle not measurable")
            out.append(piece + (" (cut at picture edge)" if "was cut at the" in body else ""))
        return "; ".join(out)[:150] or sub(first)[:90]
    if name == "measure":
        spots = re.findall(r"spot (\d) \(x=\d+, y=\d+\): ([\d.]+) m (ahead|behind you), ([\d.]+) m to the (left|right)", t)
        out = [f"spot {n}: {a} {'ahead' if w == 'ahead' else 'behind'}, {l} {side}" for n, a, w, l, side in spots]
        bad = [m for m in re.findall(r"spot (\d) \(x=\d+, y=\d+\): (that spot is [^.(]*)", t)]
        out += [f"spot {n}: not measurable ({'grey padding' if 'padding' in why else 'off the table'})" for n, why in bad]
        ang = re.search(r"angle ([+-]\d+) degrees", t)
        if ang:
            out.append(f"line angle {ang.group(1)} deg")
        return "; ".join(out)[:150] or sub(first)[:90]
    return sub(first)[:110]


def _first_line(text, n=110):
    return (text or "").split("\n")[0][:n]


# =============================================================================== one episode
def run_episode(duck, brain, tools, referee, log, ep, scene_id, command, system_prompt, tool_schemas, opening, max_steps=40,
                guard_on=True, success_fn=None, kick_after=0, verbose=True, tag=None, title=None, detail="full"):
    """Returns the episode's result dict. `opening(command)` -> the text of the first message. `duck.reset(seed=scene_id)` starts the scene.
    tag = the start of the picture file names (default "s<scene>"; learn.py gives every attempt its own); title = the transcript heading.
    The returned dict also holds the whole conversation under "_messages" (learn.py reads it to let the duck reflect on its attempt).
    detail = what is PRINTED: "full" (every step with its note, the vision model's raw answer, the pointing check ...) or "compact" (one line per step
    with the result in numbers, one line for the duck's note, and only the guard / safety events). The files in the log folder are the same either way."""
    compact = detail == "compact"
    tag = tag or f"s{scene_id:02d}"
    success_fn = success_fn or (lambda d: d.banana_in_box())
    duck.reset(seed=scene_id)
    tools.reset()
    referee.start()
    guard = Guard(guard_on, max_failed=MAX_FAILED_GOALS, max_sim_s=MAX_SIM_S)
    stall = StallWatch(guard_on)
    stall.start(referee.now)
    if hasattr(brain, "new_episode"):
        brain.new_episode()
    sim0, wall0 = duck.t, time.time()
    msgs = [{"role": "system", "content": system_prompt}, {"role": "user", "content": opening(command)}]
    log.event("episode_start", ep, t_sim=0.0, scene=scene_id, command=command, guard=guard_on, kick_after=kick_after,
              truth=truth(duck), messages=_loggable(msgs))
    log.transcript(f"\n## {title or f'Scene {scene_id}'}   (command: {command})\n")
    sent, llm_s, ptok, gtok, bad, nsteps, gtrig, rejected, reason = 0, 0.0, 0, 0, 0, 0, 0, 0, "max_steps"
    tool_counts, first_tool, kicked = {}, None, None
    ctx_said = False

    def note(i, name, text):
        nonlocal gtrig
        gtrig += 1
        log.event("guard", ep, i, duck.t - sim0, name=name, text=text)
        log.transcript(f"- GUARD {name}: {text}")
        if verbose:
            print(f"           ## guard: {name} - {text[:110] if compact else text}")

    try:
        for i in range(1, max_steps + 1):
            log.event("llm_request", ep, i, duck.t - sim0, new_messages=_loggable(msgs[sent:]), n_messages=len(msgs))
            t0 = time.time()
            try:
                resp = chat_with_retry(brain, msgs, tool_schemas)
            except LLMError as e:
                reason = "llm_error"
                log.event("llm_error", ep, i, duck.t - sim0, error=str(e))
                if verbose:
                    print(f"  !! the LLM call failed twice: {e}")
                break
            dt = time.time() - t0
            sent = len(msgs)
            llm_s += dt
            m = resp.get("message", {})
            thought = m.get("thinking") or ""
            pt, gt = resp.get("prompt_eval_count"), resp.get("eval_count")
            ptok += pt or 0
            gtok += gt or 0
            log.event("llm_response", ep, i, duck.t - sim0, llm_s=round(dt, 3), message=m, prompt_tokens=pt, gen_tokens=gt,
                      prompt_eval_ms=round(resp.get("prompt_eval_duration", 0) / 1e6, 1), gen_ms=round(resp.get("eval_duration", 0) / 1e6, 1),
                      done_reason=resp.get("done_reason"), context=resp.get("_ctx"))
            ctx = getattr(brain, "num_ctx", None)
            if ctx and pt and pt >= CTX_WARN * ctx:
                log.event("context_warning", ep, i, duck.t - sim0, prompt_tokens=pt, num_ctx=ctx)
                if verbose and not (compact and ctx_said):
                    ctx_said = True
                    print(f"           !! the prompt ({pt} tokens) nearly fills the context window ({ctx}): the oldest steps are dropped"
                          + (" (said once per attempt)" if compact else ""))
            calls = m.get("tool_calls") or []
            common = dict(episode=ep, scene=scene_id, step=i, llm_s=round(dt, 3), prompt_tokens=pt, gen_tokens=gt, think_chars=len(thought))
            log.transcript(f"### step {i}   ({dt:.1f} s of LLM time, {pt} prompt tokens)")
            if thought:
                log.transcript("THINKING: " + thought.strip().replace("\n", "\n          "))
            if not calls:                                          # the model talked instead of calling a tool
                bad += 1
                text = (m.get("content") or "").strip()
                tr = referee.now
                log.event("no_tool_call", ep, i, duck.t - sim0, text=text)
                log.transcript(f"NO TOOL CALL. It said: {text[:400]}")
                log.step_row(tool="(none)", result=text[:300].replace("\n", " | "), sim_s=0, t_sim=round(duck.t - sim0, 2),
                             t_run=round(time.time() - log.t0, 2), holding_after=tr["holding"], **{k: tr[k] for k in POSITION_KEYS}, **common)
                if verbose:
                    print((f"  {i:2d} [{dt:3.0f}s] (no tool call) {text[:60]!r}   [missed]" if compact else f"  step {i:2d} [{dt:5.1f}s]  (no tool call) {text[:80]!r}"))
                if bad >= 3:
                    reason = "no_tool_calls"
                    break
                msgs.append({"role": "assistant", "content": text})
                msgs.append({"role": "user", "content": "Reply by calling exactly one tool."})
                continue
            fn = calls[0].get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if len(calls) > 1:
                log.event("extra_tool_calls_ignored", ep, i, duck.t - sim0, ignored=[c.get("function", {}).get("name") for c in calls[1:]])
            first_tool = first_tool or name
            tool_counts[name] = tool_counts.get(name, 0) + 1
            s0 = duck.t
            log.event("tool_call", ep, i, duck.t - sim0, tool=name, args=args)
            result = tools.call(name, args)
            text = result.text
            ground = result.meta.get("grounding")                # `find`: the vision model's own answer is logged in full
            if ground:
                llm_s += ground.get("llm_s", 0.0)
                log.event("grounding", ep, i, duck.t - sim0, what=ground["what"], raw=ground["raw"], llm_s=ground["llm_s"], boxes=ground["boxes"])
            v = referee.after_call(name, args, result.meta)
            nsteps += 1
            # --- guardrails: what the safety layer did inside the tool, the false-done rejection, the loop / budget decision
            fired = [t["guard"] for t in tools.triggers]
            for t in tools.triggers:
                note(i, t["guard"], t["detail"])
            tools.triggers.clear()
            if name == "done" and tools.finished and guard_on and not success_fn(duck) and rejected < MAX_DONE_REJECTS:
                rejected += 1
                tools.finished = False
                text = "Not finished: the command is not completed yet. Look again and continue."
                fired.append("done_rejected")
                note(i, "done_rejected", f"attempt {rejected}")
            action, info = guard.after_call(v, referee.budget_failures, duck.t - sim0)      # failed MOVES since the last grasp (referee.py)
            if action == "look":
                fired.append("loop_warning")
            elif action == "abort":
                fired.append(info)
            s_act, s_info = stall.after_call(v["after"])          # nothing changed for 5 calls: warn; for 10: stop
            if s_act == "warn":
                fired.append("stall_warning")
            elif s_act == "abort" and action != "abort":
                fired.append(s_info)
            # --- the frame is saved as a file and goes to the model as a real picture
            frame_file = ""
            if result.image:
                frame_file = log.save_frame(f"{tag}_{i:02d}_{name}.jpg", result.image)
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [calls[0]]})
            msgs.append({"role": "tool", "tool_name": name, "content": text})
            if result.image:
                msgs.append({"role": "user", "content": result.caption, "images": [base64.b64encode(result.image).decode()]})
            check = {k: v[k] for k in ("call", "expected", "actual", "effect_ok", "unexpected", "changes", "repeat", "loop", "pointing")}
            log.event("tool_result", ep, i, duck.t - sim0, tool=name, result=text, check=check, frame=frame_file,
                      sim_s=round(duck.t - s0, 2), truth_before=v["before"], truth_after=v["after"])
            after = v["after"]
            point = "; ".join(f"spot {p['spot']}: " + (f"{p['nearest']} {p['err_cm']} cm" if p["nearest"] else "no point") for p in (v["pointing"] or []))
            log.step_row(tool=name, args=json.dumps({k: val for k, val in args.items() if k != "note"}), effect_ok=v["effect_ok"],
                         expected=v["expected"], actual=v["actual"], loop=v["loop"], unexpected="; ".join(v["unexpected"]),
                         guard="; ".join(fired), pointing=point, result=text.replace("\n", " | "), sim_s=round(duck.t - s0, 2),
                         t_sim=round(duck.t - sim0, 2), t_run=round(time.time() - log.t0, 2), holding_after=after["holding"],
                         frame=frame_file, **{k: after[k] for k in POSITION_KEYS}, **common)
            log.transcript(f"CALL:   {name}({', '.join(f'{k}={val}' for k, val in args.items())})")
            log.transcript("RESULT: " + text.replace("\n", "\n        ") + (f"\n        [picture: {frame_file}]" if frame_file else ""))
            if ground:
                log.transcript(f"        (vision model, {ground['llm_s']} s, raw answer: {' '.join(ground['raw'].split())[:300]})")
            log.transcript(f"REFEREE: {'ok' if v['effect_ok'] else 'GOAL MISSED'} - expected {v['expected']}; got {v['actual']}"
                           + (f"   POINTING {point}" if point else "") + (f"   UNEXPECTED {'; '.join(v['unexpected'])}" if v["unexpected"] else "") + "\n")
            if verbose and compact:
                shown = ", ".join(f"{k}={val}" for k, val in args.items() if k != "note" and not (k == "turn" and not val))
                print(f"  {i:2d} [{dt:3.0f}s] {name}({shown}) -> {brief(name, text)}" + ("" if v["effect_ok"] else "   [missed]"))
                if args.get("note"):
                    print(f"        note: {' '.join(str(args['note']).split())[:110]}")
                if v["unexpected"]:
                    print(f"        !! unexpected: {'; '.join(v['unexpected'])[:110]}")
            elif verbose:
                shown = ", ".join(f"{k}={val}" for k, val in args.items() if k != "note")
                print(f"  step {i:2d} [{dt:5.1f}s]  {name}({shown})  ->  {_first_line(text)}")
                if thought:
                    print(f"           thought: {' '.join(thought.split())[:150]}")
                if args.get("note"):
                    print(f"           note: {str(args['note'])[:150]}")
                if ground:
                    print(f"           vision model answered: {' '.join(ground['raw'].split())[:150]}")
                if point:
                    print(f"           pointing: {point}")
                if not v["effect_ok"]:
                    print(f"           !! goal missed: expected {v['expected']}; got {v['actual']}")
                if v["unexpected"]:
                    print(f"           !! unexpected: {'; '.join(v['unexpected'])}")
                if v["loop"]:
                    print(f"           !! loop: {v['loop']} (x{v['repeat']})")
            if tools.finished:
                reason = "done"
                break
            if duck.is_fallen() or duck.off_table():
                reason = "fell" if duck.is_fallen() else "off_table"
                break
            # --- fault test: knock the banana away (only if it lies on the table); the duck is NOT told - it has to notice with its camera
            if kick_after and kicked is None and nsteps >= kick_after and not duck.holding and not success_fn(duck):
                kicked = kick_banana(duck)
                if kicked:
                    log.event("fault_injected", ep, i, duck.t - sim0, fault="banana_kick", new_xy=[round(c, 3) for c in kicked])
                    if verbose:
                        print(f"  >> FAULT: the banana was knocked to ({kicked[0]:.2f}, {kicked[1]:.2f})")
            if action == "abort":
                reason = info
                note(i, info, "episode stopped")
                break
            if s_act == "abort":
                reason = s_info
                note(i, s_info, f"nothing changed for {stall.n} tool calls in a row - episode stopped")
                break
            if action == "look":
                msgs.append({"role": "user", "content": "[guard] " + info})
                note(i, "loop_warning", info)
            if s_act == "warn":
                msgs.append({"role": "user", "content": "[guard] " + s_info})
                note(i, "stall_warning", s_info)
    except ViewerClosed:
        raise
    except Exception as e:                                       # a bug or a simulator error ends THIS scene, not the whole run
        reason = "crash"
        log.event("episode_crash", ep, duck.t - sim0, error=repr(e), traceback=traceback.format_exc())
        log.transcript(f"CRASH in this scene: {e!r}  (see events.jsonl)")
        print(f"  !! this scene crashed: {e!r}  - details in events.jsonl; the run goes on")
    final = truth(duck)
    success = bool(success_fn(duck))
    c = referee.counts
    res = {"scene": scene_id, "success": success, "claimed_done": tools.finished, "false_claim": bool(tools.finished and not success),
           "ended": reason, "steps": nsteps, "first_tool": first_tool, "tool_counts": json.dumps(tool_counts, sort_keys=True),
           "no_tool_calls": bad, "llm_s": round(llm_s, 1), "prompt_tokens": ptok, "gen_tokens": gtok,
           "sim_s": round(duck.t - sim0, 1), "wall_s": round(time.time() - wall0, 1), "guard_actions": gtrig, "done_rejected": rejected,
           "final_dist_banana_to_box": round(math.hypot(final["banana_x"] - final["box_x"], final["banana_y"] - final["box_y"]), 3),
           "command": command, **c}
    log.event("episode_end", ep, t_sim=duck.t - sim0, result=res, truth=final)
    log.transcript(f"RESULT: {'SUCCESS' if success else 'NOT SUCCESSFUL'} - ended: {reason}, {nsteps} tool calls, sim {res['sim_s']} s, LLM {res['llm_s']} s")
    log.episode_row(episode=ep, model=getattr(brain, "model", "?"), **res)
    res["_messages"] = msgs
    return res
