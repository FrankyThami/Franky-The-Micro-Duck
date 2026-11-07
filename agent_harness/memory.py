"""memory.py - Harness v1, part A4: Franky's notebook (episodic reflection -> lessons).

The model's WEIGHTS NEVER CHANGE. What is "learned" is a short list of rules that Franky writes for himself after an
episode that went wrong or wasted steps. They are kept in a plain JSON file (you can read and edit it) and the best few
are added to the system prompt of later episodes.

    episode  ->  log of what Franky saw and did  ->  "write ONE rule for your future self"  ->  notebook (JSON)
    next episode: system prompt + the top lessons from the notebook

  LessonStore  the notebook: add (with duplicate merging), pick the top lessons, save
  MemoryBrain  wraps a brain: puts the top lessons into the system prompt of every request
  reflect()    asks the model for one rule after a bad episode
"""

import json
import os
import re
import time

MAX_LESSONS_SHOWN = 3
MAX_LESSON_CHARS = 160
MERGE_SIMILARITY = 0.6          # two lessons sharing this share of their words count as the same lesson

REFLECT_SYSTEM = ("You are Franky, a small duck robot. You are reading the log of your last attempt at the task "
                  "'put the banana into the green box'. You write short rules for your future self.")
REFLECT_QUESTION = ("\n\nWrite ONE rule (one sentence, at most 25 words, starting with 'If' or 'Before') that would have "
                    "avoided the wasted or failed steps. Reply with only that sentence.")


# =============================================================================== helpers
def clean(raw):
    """Turn the model's answer into one short sentence, or '' if it is not usable."""
    t = " ".join((raw or "").replace("*", " ").split())
    t = re.sub(r"^(?:[-\u2022]|\d+[.)])\s*", "", t).strip(" \"'")
    t = re.sub(r"^(?:rule|lesson)\s*:\s*", "", t, flags=re.I)
    t = re.split(r"(?<=[.!?])\s+", t)[0].strip()
    if not 15 <= len(t) <= MAX_LESSON_CHARS or any(c in t for c in "{}`"):
        return ""
    return t


def _words(text):
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def similar(a, b):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= MERGE_SIMILARITY


# =============================================================================== the notebook
class LessonStore:
    def __init__(self, path, fresh=False):
        self.path, self.seq = path, 0
        self.lessons = []
        if not fresh and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.lessons = json.load(f).get("lessons", [])
            self.seq = max([l.get("seq", 0) for l in self.lessons] + [0])
        if fresh:
            self.save()

    def add(self, text, run_id):
        """-> ('new' | 'merged', entry)"""
        self.seq += 1
        for l in self.lessons:
            if similar(l["text"], text):
                l["count"] += 1
                l["seq"], l["last_run"] = self.seq, run_id
                self.save()
                return "merged", l
        entry = {"text": text, "count": 1, "seq": self.seq, "first_run": run_id, "last_run": run_id,
                 "added": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.lessons.append(entry)
        self.save()
        return "new", entry

    def top(self, k=MAX_LESSONS_SHOWN):
        """The lessons seen most often first; among equals the most recent one."""
        return sorted(self.lessons, key=lambda l: (-l["count"], -l["seq"]))[:k]

    def prompt_block(self, k=MAX_LESSONS_SHOWN):
        top = self.top(k)
        if not top:
            return ""
        return "\n\nLessons you wrote for yourself after earlier attempts (follow them):\n" + "\n".join("- " + l["text"] for l in top)

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"note": "Franky's notebook. Written by the model itself; the weights never change.",
                       "lessons": self.lessons}, f, indent=2, ensure_ascii=False)


# =============================================================================== the brain wrapper
class MemoryBrain:
    """Wraps a brain. With a store, the top lessons are added to the system prompt of every request.
    Without a store (store=None) it changes nothing - that is the 'memory OFF' condition."""

    def __init__(self, brain, store=None, k=MAX_LESSONS_SHOWN):
        self.brain, self.store, self.k = brain, store, k
        self.model = brain.model
        self.num_ctx = getattr(brain, "num_ctx", None)
        self.kind = type(brain).__name__
        self.last_msgs = None                     # the whole conversation of the current episode (same list the harness fills)

    def warmup(self):
        return self.brain.warmup()

    def lessons_shown(self):
        return [l["text"] for l in self.store.top(self.k)] if self.store else []

    def chat(self, msgs, tools=None, **kw):
        self.last_msgs = msgs
        block = self.store.prompt_block(self.k) if self.store else ""
        if block and msgs and msgs[0].get("role") == "system":
            msgs = [{**msgs[0], "content": msgs[0]["content"] + block}] + msgs[1:]
        return self.brain.chat(msgs, tools, **kw)


# =============================================================================== reflection
def episode_digest(msgs, res):
    """What Franky saw and did, as short numbered lines (only what he could see himself) + how it ended."""
    lines, pending, n = [], None, 0
    for m in msgs[2:]:
        if m["role"] == "assistant" and m.get("tool_calls"):
            fn = m["tool_calls"][0]["function"]
            pending = f"{fn.get('name')}({', '.join(str(v) for v in (fn.get('arguments') or {}).values())})"
        elif m["role"] == "tool" and pending:
            n += 1
            lines.append(f"{n}. {pending} -> {m['content'].splitlines()[0][:150]}")
            pending = None
        elif m["role"] == "user" and m["content"].startswith("[status]"):
            lines.append("   (" + m["content"].splitlines()[0][:150] + ")")
    end = ("the banana ended up in the green box" if res["success"]
           else f"FAILED - the banana did not end up in the green box (episode ended: {res['ended']})")
    return "Log of your attempt:\n" + "\n".join(lines) + f"\nOutcome: {end}; {res['steps']} tool calls used."


def reflect(brain, store, msgs, res, run_id):
    """After an episode that went wrong or wasted steps: ask the model for ONE rule and save it. Never raises."""
    if res["success"] and not res["failed_effects"] and not res["failed_calls"]:
        return {"status": "skipped: nothing went wrong"}
    digest = episode_digest(msgs, res)
    try:
        resp = brain.chat([{"role": "system", "content": REFLECT_SYSTEM},
                           {"role": "user", "content": digest + REFLECT_QUESTION}], None, num_predict=80)
    except (OSError, RuntimeError, ValueError) as e:
        return {"status": f"failed: {e}", "digest": digest}
    raw = (resp.get("message", {}).get("content") or "").strip()
    text = clean(raw)
    if not text:
        return {"status": "rejected (not a usable sentence)", "raw": raw, "digest": digest}
    status, entry = store.add(text, run_id)
    return {"status": status, "lesson": entry["text"], "count": entry["count"], "raw": raw, "digest": digest}