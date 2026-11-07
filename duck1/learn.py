"""learn.py - Franky LEARNS: one scene at a time, with a notebook it writes itself.

    python duck1/learn.py                              # scene 0 (gemma4:e4b, square picture): attempt, reflect, write notes, try again - until it works
    python duck1/learn.py --scenes 0 1 2 3             # a curriculum: the duck moves on to the next scene only when it has solved this one
    python duck1/learn.py --scenes 13 12 10 11 0       # PRACTICE ladder first, then scene 0. 13: the duck already holds the banana, the box is out of view on its right;
                                                       # 12: the duck starts close to a banana that lies across its heading; 10: the same from the usual start; 11: banana turned
                                                       # 45 degrees. The duck moves up a rung only after it solved the one before. The practice scenes use a wider grasp window
                                                       # (told to the duck as 7 cm / 30 degrees; scene 0 keeps 5 cm / 20): see scenes.py
    python duck1/learn.py --attempts 8 --need 2        # up to 8 attempts per scene; a scene counts as learned after 2 successes in a row
    python duck1/learn.py --view                       # watch it in the MuJoCo window
    python duck1/learn.py --learning off               # CONTROL: the same repeated attempts, but no notebook (does the notebook help at all?)
    python duck1/learn.py --learning frozen            # the notebook is read but not changed (does what it learned work in a new scene?)
    python duck1/learn.py --fresh                      # start with an empty notebook (the old one is kept as franky_notebook.json.old_<time>)
    python duck1/learn.py --feedback off               # the duck is NOT told whether the banana ended up in the box
    python duck1/learn.py --think off                  # about 3-5x faster per step (thinking is ~70 % of the tokens), the duck reasons less
                                                       # (the reflection after each attempt still THINKS: --reflect-think off to stop that too)
    python duck1/learn.py --beak-sense off             # CONTROL: close_beak does not report where the banana was (the version-4 message)
    python duck1/learn.py --turn-tool off              # CONTROL: the seven tools of versions 1-11 (no `turn`; walk turns, positive = left)
    python duck1/learn.py --forget-quote off           # CONTROL: the old FORGET (an id is enough; version 13 wants a quote from the log with it)
    python duck1/learn.py --keep-notes old             # CONTROL: the old way of making room (the weakest old note goes at once; version 13: a note gets 3 attempts)
    python duck1/learn.py --detail full                # the long terminal output (default: compact, one line per tool call; the files on disk are the same)

HOW IT LEARNS (the model's weights never change - it is a notebook, like a person's):
  1. ATTEMPT   the duck gets the command and its notebook (its own notes from earlier attempts) and does the scene exactly as in the benchmark:
               the same tools, the same camera, the same safety and guards. Nothing about the scene is told to it.
  2. REFLECT   when the attempt is over, the duck reads the log of what it did (each tool, what came back, its own notes, the warnings it got) and
               what the person says about the result ("the banana is NOT in the green box"), and writes notes:
                   LEARNED: a fact about its body, tools or the world        SKILL: a way of doing something, as short instructions to itself
                   FORGET:  notes that this log shows to be wrong
  3. NOTEBOOK  the notes go into outputs\\memory\\franky_notebook.json (and a readable .md). Duplicates are merged, the notebook stays small
               (10 facts, 6 skills). For every note the file counts the attempts it was shown in (tries), how many of them succeeded (wins) and
               how often the duck wrote it down again (seen). Version 13: FORGET needs a quote from the log (the code checks the quote is in the
               log, nothing more), and a note can only be pushed out after 3 attempts, the least often written one first.
  4. AGAIN     next attempt, same scene: the notebook is in the duck's first message, before it looks at anything. When the scene is solved
               (--need successes in a row) the NEXT scene starts with the same notebook: how many attempts does it need there?
Nothing in the code writes, ranks or corrects a note: what is in the notebook is what the model decided to write.

WHAT THE DUCK IS TOLD ABOUT THE RESULT: only the person's verdict after an attempt (banana in the box or not; whether it fell off the table; whether
it still holds it) - the true state, but only as an outcome, like a person watching. Never positions, never what to do. --feedback off removes it.
COMPARE: run the same scene with --learning off (no notebook) and with the default; if the notebook helps, "attempts to solve" is lower with it.

Logs: outputs\\logs\\duck1\\run_<time>_..._learn\\   transcript.md (all attempts, with the notebook the duck was given and its reflection),
      learning.csv (one row per attempt: success, tool calls, notes added ...), notebook_after_s00_a01.json (the notebook after every attempt),
      frames\\ (s00_a01_03_find.jpg = scene 0, attempt 1, step 3), steps.csv, episodes.csv, events.jsonl, run_meta.json.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path

import csv
import json
import os
import re
import time

from agent_harness.agent_loop import AgentLogger, run_episode
from duck1 import scenes
from duck1.franky_memory import (Notebook, REFLECT_SYSTEM, attempt_digest, ended_text, evidence_text, parse_reflection, reflect_format,
                                 reflection_messages, verdict_text)
from duck1.franky_tools import DEFAULT_WINDOW, SYSTEM_PROMPT, body_facts, grasp_window, opening_message, stated_window, texts_for, tools_for, window_words
from duck1.scene_benchmark import build_parser, build_world, start_brain, write_meta
from project_paths import MEMORY_DIR
from sim.duck_sim import ViewerClosed

CSV_COLS = ["scene", "scene_name", "attempt", "success", "steps", "ended", "walks", "turns", "close_beaks", "finds", "failed_effects", "llm_s", "wall_s",
            "notes_shown", "reflect_s", "reflect_mode", "added", "merged", "forgotten", "forget_refused", "evicted", "rejected", "verdict"]


def build_learn_parser():
    ap = build_parser("Franky learns one scene at a time, with a notebook it writes itself.")
    ap.set_defaults(model="gemma4:e4b", pad_square="on", scenes=[0])             # the settings pointing_check.py found best (e4b, square picture)
    g = ap.add_argument_group("learning")
    g.add_argument("--attempts", type=int, default=6, help="attempts per scene before giving up on it (default 6)")
    g.add_argument("--need", type=int, default=1, help="successes in a row that make a scene 'learned' (default 1)")
    g.add_argument("--on-fail", choices=["stop", "continue"], default="stop",
                   help="when a scene is not solved within --attempts: stop the run (default) or go on to the next scene anyway")
    g.add_argument("--learning", choices=["on", "frozen", "off"], default="on",
                   help="on = read the notebook and write to it (default); frozen = read it, never change it; off = no notebook at all (control run)")
    g.add_argument("--feedback", choices=["on", "off"], default="on",
                   help="on = after an attempt the duck is told whether the banana is in the green box (default); off = it is not told")
    g.add_argument("--memory", default=os.path.join(MEMORY_DIR, "franky_notebook.json"), help="the notebook file (JSON; a readable .md is written next to it)")
    g.add_argument("--fresh", action="store_true", help="start with an empty notebook (the old file is renamed, not deleted)")
    g.add_argument("--reflect-think", choices=["on", "off"], default="on",
                   help="on = the duck THINKS when it reflects, also in a --think off run (default); off = it writes its notes straight away. "
                        "The reflection is one call per attempt, so thinking costs a few minutes per attempt, not per step")
    g.add_argument("--reflect-tokens", type=int, default=2400, help="most tokens the duck may write when it reflects (thinking + notes)")
    g.add_argument("--forget-quote", choices=["on", "off"], default="on",
                   help="on = a FORGET line must carry a quote copied from the log, and the code checks the quote is there (default, version 13); "
                        "off = an id is enough (the old FORGET)")
    g.add_argument("--keep-notes", choices=["fair", "old"], default="fair",
                   help="fair = a note is shown in 3 attempts before a new note may push it out, the least often written one goes first, and a new note is "
                        "not added when no note has had its 3 tries (default, version 13); old = the weakest old note (wins x 3 + times written) goes at once")
    return ap


def _counts(res):
    try:
        return json.loads(res.get("tool_counts") or "{}")
    except ValueError:
        return {}


def _short(text, n=190):
    return " ".join((text or "").split())[:n]


def closes_line(msgs):
    """One line for the terminal: what happened at every close of the beak in this attempt (from the tool results the duck itself got).
    far = the banana was more than 0.10 m from the beak tip; otherwise the banana's place relative to the beak tip and the angle of its long side
    (the grasp needs it across: about +-90, within 20)."""
    out = []
    for m in msgs:
        if m.get("role") != "tool" or m.get("tool_name") not in ("close_beak", "pick"):
            continue
        t = m.get("content") or ""
        if "closed on the banana" in t:
            g = re.search(r"points ([+-]\d+) degrees", t)
            out.append("TAKEN" + (f" (long side {g.group(1)} deg)" if g else ""))
            continue
        g = re.search(r"was ([\d.]+) m (ahead of|behind) your beak tip and ([\d.]+) m to the (left|right) of it.*?points ([+-]\d+) degrees", t)
        if g:
            out.append(f"{g.group(1)} m {'ahead' if g.group(2) == 'ahead of' else 'behind'}/{g.group(3)} m {g.group(4)}/{g.group(5)} deg")
        elif "more than" in t:
            out.append("far")
    if not out:
        return "beak closes: none"
    far = out.count("far")
    near = [o for o in out if o != "far"]
    return f"beak closes: {len(out)}" + (f", {far} from farther than 0.10 m" if far else "") + (f"; near ones (ahead/side/long side): {', '.join(near[:6])}" if near else "")


def reflect_on(brain, notebook, args, duck, res, source, log, ep, say=print, window=None, hold=False, facts=""):
    """The duck reflects on the attempt that just ended and its notes go into the notebook. -> dict for the log. Never raises."""
    digest = attempt_digest(res["_messages"])
    ended = ended_text(res, args.max_steps)
    verdict = verdict_text(duck, res) if args.feedback == "on" else ""
    note = ("" if window is None or window == DEFAULT_WINDOW else
            f"In this scene your beak took the banana when its middle was within {window[0] * 100:.0f} cm of your beak tip and its long side was within "
            f"{window[1]:.0f} degrees of lying exactly across your beak (a wider window than in other scenes).")
    if hold:
        note = (note + " " if note else "") + "In this scene the attempt began with the banana already held in your beak."
    quote_rule = getattr(args, "forget_quote", "off") == "on"
    msgs = reflection_messages(args.command, notebook.block(), digest, ended, verdict, note, facts, forget_quote=quote_rule)
    info = {"mode": "failed", "secs": 0.0, "raw": "", "thinking": "", "added": [], "merged": [], "forgotten": [], "evicted": [], "rejected": [],
            "digest": digest, "verdict": verdict}
    think = getattr(args, "reflect_think", "on") == "on"
    say("  reflecting on the attempt " + ("(the duck thinks it over: about 2-4 minutes, nothing prints until it is done - please wait, Ctrl+C loses the notes) ..."
                                         if think else "(about 1 minute - please wait) ..."))
    try:
        text, thinking, secs = brain.complete(msgs, think=think, num_predict=args.reflect_tokens)
    except Exception as e:                                          # Ollama hiccup: the attempt still counts, the notebook just stays as it was
        info["raw"] = f"ERROR {e}"
        say(f"  !! the reflection failed: {e}")
        log.event("reflection", ep, mode="failed", error=str(e))
        return info
    parsed = parse_reflection(text)
    changes = notebook.learn(parsed, source, log_text=evidence_text(digest, ended, verdict) if quote_rule else None,
                             fair=getattr(args, "keep_notes", "old") == "fair")
    info.update(mode=parsed["mode"], secs=round(secs, 1), raw=text, thinking=thinking, added=changes["added"], merged=changes["merged"],
                forgotten=changes["forgotten"], evicted=changes["evicted"], rejected=changes["rejected"])
    log.event("reflection", ep, mode=parsed["mode"], secs=round(secs, 2), raw=text, thinking=thinking, prompt=msgs[1]["content"],
              added=changes["added"], merged=[m["id"] for m in changes["merged"]], forgotten=[f["id"] for f in changes["forgotten"]],
              forget_quotes={f["id"]: f.get("forget_quote", "") for f in changes["forgotten"]},
              evicted=[e["id"] for e in changes["evicted"]], rejected=changes["rejected"])
    log.transcript("### REFLECTION  (the duck reads its log and writes notes)\n" + (f"THINKING: {thinking.strip()[:1500]}\n" if thinking else "")
                   + f"ANSWER:\n{text.strip()}\n"
                   + f"NOTEBOOK CHANGES: +{len(changes['added'])} new, {len(changes['merged'])} merged with an existing note, {len(changes['forgotten'])} forgotten, "
                   f"{len(changes['evicted'])} pushed out"
                   + ("".join(f"\n   rejected: {_short(r[0], 100)!r} ({r[1]})" for r in changes["rejected"]) if changes["rejected"] else "") + "\n")
    say(f"  notebook ({secs:.0f} s of reflection): +{len(changes['added'])} new, {len(changes['merged'])} merged, "
        f"{len(changes['forgotten'])} forgotten, {len(changes['evicted'])} pushed out" + ("" if parsed["mode"] == "lines" else f"   [answer read as: {parsed['mode']}]"))
    compact = getattr(args, "detail", "full") == "compact"
    for it in changes["added"]:
        say(f"     + {it['id']}: {_short(it['text'], 150 if compact else 230)}")
    for it in changes["merged"]:
        say(f"     = {it['id']} written again (seen {it['seen']}x)")
    for it in changes["forgotten"]:
        say(f"     - {it['id']} forgotten: {_short(it['text'], 120)}" + (f'   (quote from the log: "{_short(it["forget_quote"], 90)}")' if it.get("forget_quote") else ""))
    for raw, why in changes["rejected"][:3]:
        say(f"     (not used: {_short(raw, 80)!r} - {why})")
    return info


def run_learning(args, brain, w, log, notebook, ids, say=print, summaries=None):
    """The curriculum. brain = the LLM client (chat / complete); w = the world (duck, tools, referee, cap, rec ...).
    -> list of per-scene summaries [{"scene", "attempts", "solved", "results": [...]}] (filled in as it goes, so a Ctrl+C keeps what is done)"""
    duck, tools, referee, cap, rec = w.duck, w.tools, w.referee, w.cap, w.rec
    show = getattr(cap, "show", lambda *a, **k: None)
    f = open(os.path.join(log.dir, "learning.csv"), "w", newline="", encoding="utf-8-sig")
    out = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore", restval="")
    out.writeheader()
    summaries = [] if summaries is None else summaries
    ep = 0
    use_notebook = args.learning in ("on", "frozen")
    try:
        for sid in ids:
            sc = scenes.SCENES[sid]
            say(f"\n{'=' * 100}\n=== scene {sid}: {sc['name']}   banana {sc['banana']}  box {sc['box']}  ({sc['note']}) ===")
            gw = stated_window(sc)                                      # what the duck is told; the simulator's radius is a little larger (3-D, see franky_tools)
            if gw != DEFAULT_WINDOW:
                say(f"    grasp window in this scene: {window_words(gw)} as told to the duck, simulator radius {grasp_window(sc)[0] * 100:.0f} cm  "
                    f"(the benchmark scenes use {window_words(DEFAULT_WINDOW)})")
            summ = {"scene": sid, "name": sc["name"], "attempts": 0, "solved": False, "solved_at": None, "results": []}
            summaries.append(summ)
            streak = 0
            for attempt in range(1, args.attempts + 1):
                ep += 1
                block = notebook.block() if use_notebook else ""
                shown = notebook.shown_ids() if use_notebook else []
                brain.seed = args.seed + ep - 1                         # a new sampling seed per attempt: a retry is never a literal repeat
                say(f"\n--- scene {sid}, attempt {attempt} of {args.attempts}   (notebook shown to the duck: "
                    + (f"{len([i for i in shown if i[0] == 'K'])} facts, {len([i for i in shown if i[0] == 'S'])} skills" if shown else "none") + ") ---")
                log.transcript(f"\n# ===== SCENE {sid}  ATTEMPT {attempt}  (notebook shown: {', '.join(shown) or 'none'}) =====")
                if gw != DEFAULT_WINDOW:
                    log.transcript(f"GRASP WINDOW IN THIS SCENE: {window_words(gw)} as told to the duck, simulator radius {grasp_window(sc)[0] * 100:.0f} cm (benchmark: {window_words(DEFAULT_WINDOW)})")
                if block:
                    log.transcript("NOTEBOOK GIVEN TO THE DUCK:\n" + block + "\n")
                cap.title, cap.n = f"SCENE {sid}: {sc['name']}  attempt {attempt}", 0
                if rec:
                    rec.mark(f"scene {sid} attempt {attempt}")
                show(cap.title)
                r = run_episode(duck, cap, tools, referee, log, ep, sid, args.command, *texts_for(sc, args.find == "on", args.turn_tool == "on"),
                                lambda c, nb=block: opening_message(c, nb), max_steps=args.max_steps, guard_on=args.guard == "on",
                                kick_after=args.kick_after, tag=f"s{sid:02d}_a{attempt:02d}", title=f"Scene {sid} - attempt {attempt}",
                                detail=args.detail)
                summ["attempts"] = attempt
                say(f"  RESULT: banana in box = {r['success']}   ({r['steps']} tool calls, {r['failed_effects']} missed goals, "
                    f"{r['refusals']} refused / {r['stops']} stopped by the safety layer, ended: {r['ended']}, LLM {r['llm_s']} s, sim {r['sim_s']} s)")
                say("  " + closes_line(r["_messages"]))
                if rec:
                    rec.set_verdict(r["success"], r["sim_s"], r["steps"], r["llm_s"], r["ended"])
                if args.view:
                    show(cap.title, ("SUCCESSFUL" if r["success"] else "NOT SUCCESSFUL") + f"   {r['sim_s']} s, {r['steps']} tool calls, ended: {r['ended']}")
                if args.view or rec:
                    duck.hold(3.0)
                # ---- learning: count the attempt for the notes that were shown, then let the duck reflect
                refl = None
                if args.learning == "on":
                    notebook.record_attempt(shown, r["success"], {"scene": sid, "attempt": attempt, "steps": r["steps"], "ended": r["ended"]})
                    if r["ended"] in ("llm_error", "crash"):
                        say("  (no reflection: the attempt was cut short by an error)")
                    else:
                        refl = reflect_on(brain, notebook, args, duck, r, f"scene {sid}, attempt {attempt}", log, ep, say, window=gw,
                                         hold=bool(sc.get("hold")), facts=body_facts(texts_for(sc, args.find == "on", args.turn_tool == "on")[0]))
                    log.write_json(f"notebook_after_s{sid:02d}_a{attempt:02d}.json", notebook.as_dict())
                counts = _counts(r)
                row = {"scene": sid, "scene_name": sc["name"], "attempt": attempt, "success": r["success"], "steps": r["steps"], "ended": r["ended"],
                       "walks": counts.get("walk", 0), "turns": counts.get("turn", 0), "close_beaks": counts.get("close_beak", 0), "finds": r.get("finds", 0),
                       "failed_effects": r["failed_effects"], "llm_s": r["llm_s"], "wall_s": r["wall_s"], "notes_shown": " ".join(shown),
                       "reflect_s": refl["secs"] if refl else "", "reflect_mode": refl["mode"] if refl else "",
                       "added": " ".join(i["id"] for i in refl["added"]) if refl else "", "merged": " ".join(i["id"] for i in refl["merged"]) if refl else "",
                       "forgotten": " ".join(i["id"] for i in refl["forgotten"]) if refl else "",
                       "forget_refused": " ".join(r[0] for r in refl["rejected"] if re.fullmatch(r"[KS]\d+", r[0] or "") and str(r[1]).startswith("kept")) if refl else "",
                       "evicted": " ".join(i["id"] for i in refl["evicted"]) if refl else "", "rejected": len(refl["rejected"]) if refl else "",
                       "verdict": _short(verdict_text(duck, r), 120) if args.feedback == "on" else ""}
                out.writerow(row)
                f.flush()
                summ["results"].append({k: r[k] for k in ("success", "steps", "ended", "failed_effects", "llm_s")} | {"walks": row["walks"], "close_beaks": row["close_beaks"]})
                streak = streak + 1 if r["success"] else 0
                if streak >= args.need:
                    summ["solved"], summ["solved_at"] = True, attempt
                    say(f"\n>>> scene {sid} LEARNED after {attempt} attempt{'s' if attempt > 1 else ''}"
                        + (f" ({args.need} successes in a row)" if args.need > 1 else "") + " <<<")
                    break
            if not summ["solved"]:
                say(f"\n>>> scene {sid} NOT solved in {summ['attempts']} attempts <<<")
                if args.on_fail == "stop" and sid != ids[-1]:
                    say("    stopping here, as asked (--on-fail stop). Run it again to give the duck more attempts (the notebook is kept), or use --on-fail continue.")
                    break
    finally:
        f.close()
    return summaries


def print_curve(args, summaries, notebook):
    print("\n" + "=" * 100)
    print(f"LEARNING CURVE   model {args.model}   notebook: {args.learning}   feedback: {args.feedback}   picture: {'square' if args.pad_square == 'on' else '4:3'}")
    print(f"{'scene':<7}{'name':<16}{'learned':<10}{'attempts':>9}   tool calls per attempt  (+ = success, - = failed)")
    for s in summaries:
        marks = "  ".join(f"{r['steps']}{'+' if r['success'] else '-'}" for r in s["results"])
        got = f"YES ({s['solved_at']})" if s["solved"] else "NO"
        print(f"{s['scene']:<7}{s['name']:<16}{got:<10}{s['attempts']:>9}   {marks}")
    if args.learning != "off":
        print("-" * 100)
        print(f"notebook now: {len(notebook.knowledge)} facts, {len(notebook.skills)} skills   ->  {notebook.path}  (and a readable .md next to it)")
        for it in notebook.skills + notebook.knowledge:
            print(f"   {it['id']:<4} [{it['wins']}/{it['tries']} won]  {_short(it['text'], 120 if getattr(args, 'detail', 'full') == 'compact' else 200)}")
    print("=" * 100)


def main():
    ap = build_learn_parser()
    args = ap.parse_args()
    ids = args.scenes
    bad = [i for i in ids if not 0 <= i < len(scenes.SCENES)]
    if bad or not ids:
        raise SystemExit(f"No such scene: {bad}. Scenes are 0 to {len(scenes.SCENES) - 1}.")
    notebook = Notebook(args.memory if args.learning != "off" else None, fresh=args.fresh and args.learning != "off")
    if notebook.archived:
        print(f"the old notebook was kept as {notebook.archived}")
    print(f"notebook: {args.memory if args.learning != 'off' else '(none: --learning off)'}   "
          f"{len(notebook.knowledge)} facts, {len(notebook.skills)} skills at the start   learning: {args.learning}   feedback: {args.feedback}")
    brain = start_brain(args)
    w = build_world(args, brain, label_extra="_learn" + ("" if args.learning == "on" else f"_{args.learning}"))
    log = AgentLogger(args.logdir, w.label)
    write_meta(log, args, brain, ids)
    log.write_json("learn_meta.json", {"attempts": args.attempts, "need": args.need, "on_fail": args.on_fail, "learning": args.learning,
                                       "feedback": args.feedback, "notebook": args.memory, "notebook_at_start": notebook.as_dict(),
                                       "forget_quote": args.forget_quote, "keep_notes": args.keep_notes,
                                       "reflection_system_prompt": REFLECT_SYSTEM, "reflection_format": reflect_format(args.forget_quote == "on")})
    log.write_json("scenes.json", {str(i): scenes.SCENES[i] for i in ids})
    print(f"logs: {log.dir}", flush=True)
    summaries = []
    try:
        run_learning(args, brain, w, log, notebook, ids, summaries=summaries)
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    except KeyboardInterrupt:
        print("\nstopped with Ctrl+C - the notebook so far is saved.")
    finally:
        log.close()
        if w.rec:
            w.rec.close()
        w.camera.close()
        w.duck.close()
    if summaries:
        with open(os.path.join(log.dir, "learn_summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summaries, fh, indent=2, default=str)
        print_curve(args, summaries, notebook)
    print(f"read the transcript: {log.dir}")


if __name__ == "__main__":
    main()
