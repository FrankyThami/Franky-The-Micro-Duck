"""franky_memory.py - Franky's NOTEBOOK: what the duck writes down for itself after an attempt, and reads at the start of the next one.

The model's weights never change. What "learning" means here is exactly what a person does with a notebook:

    attempt  ->  the duck reads the log of what it did and what came back  ->  it writes notes  ->  the notebook (a JSON file)
    next attempt (same scene, or a new one): the notebook is shown to the duck in its first message, before it looks at anything

Two kinds of notes, both written by the duck in its own words (nothing in this file knows what a good note about bananas looks like):
    K1, K2 ...  "What you learned"      facts about its body, its tools and the world, with the numbers it found out
    S1, S2 ...  "Skills you worked out"  a way of doing something, as short instructions to itself

What the code does and does not do:
  * it builds the log the duck reads (tools, arguments, the first lines of what came back, the duck's own notes, the harness's warnings),
    it asks the duck to reflect, it reads the answer (lines starting LEARNED: / SKILL: / FORGET:), it cleans it, drops duplicates, keeps
    the notebook small, and counts for every note how many attempts it was shown in (tries) and how many of those succeeded (wins)
  * it does NOT write, rank or correct any note's content. A wrong note stays until the duck forgets it, or until it is pushed out.
  * VERSION 13 (after the twelfth real run, where about 25 of the 30 notes that were lost were pushed out to make room, only 5 were forgotten):
      - a FORGET line must carry a quote: the duck copies, in quotation marks, the words of the log that show the note to be wrong. The code only
        checks that the quote really is in the log of that attempt (the result lines, the harness warnings, how it ended, the person's verdict,
        NOT the duck's own [note]s); it does not judge whether the quote proves anything. (learn.py --forget-quote off = the old FORGET.)
      - every note stays for at least MIN_TRIES attempts before a newer note may push it out, and among the notes that have had their tries the
        one that the duck wrote down least often goes first (the old rule gave a note permanent protection for being in the notebook during a
        success). When no note has had its tries yet, the new note is not added. (learn.py --keep-notes old = the old rule.)
    The code still ranks only by counts (written again, tries, wins), never by what a note says.
  * the person's verdict ("the banana is in the green box" / "is NOT in the green box") is the only information about the TRUE state that
    reaches the duck, and only after the attempt (learn.py --feedback off removes it).
"""

import json
import os
import re
import time

MAX_KNOWLEDGE, MAX_SKILLS = 10, 6                  # notes kept; when a new one needs room the weakest old one goes
MAX_NEW_KNOWLEDGE, MAX_NEW_SKILLS = 3, 2           # notes taken from one reflection
MAX_FORGET = 2                                     # notes the duck may delete in one reflection
MIN_CHARS, MAX_CHARS_K, MAX_CHARS_S = 20, 300, 420
MERGE_SIMILARITY = 0.55                            # two notes sharing this share of their words count as the same note
PROTECT_WINS = 2                                   # a note that was in 2 successful attempts is not deleted by a single reflection
MIN_TRIES = 3                                      # version 13 (--keep-notes fair): a note is shown in this many attempts before a new note may push it out
MIN_QUOTE_CHARS = 12                               # version 13 (--forget-quote on): the shortest quote that counts
DIGEST_LINES = 34
DECISIVE = ("close_beak", "pick", "open_beak", "place", "done")      # the tools whose result decides the attempt: their lines are never left out

REFLECT_SYSTEM = ("You are Franky, a small duck robot. You keep a notebook. After each attempt at a command you read the log of what you did "
                  "and write down what you learned, so that your next attempts go better, also in other scenes. Your notes are short, "
                  "concrete and honest: you only write what the log shows.")

REFLECT_FORMAT = (
    "Think it through before you write: (1) what did you expect each action to do, and what did it really do? Use the numbers in the log. "
    "(2) If the beak closed more than once, compare the closes: what was different, in the numbers, between a close that took the banana "
    "and the ones that did not? (3) Which of your own notes, or of your notebook lines, does the log contradict?\n"
    "Then update your notebook. Reply with note lines only, in these forms:\n"
    "LEARNED: a fact you found out about your body, your tools or the world, with numbers if you have them.\n"
    "SKILL: a way of doing something that worked, or that you now think would work, written as short instructions to yourself.\n"
    "FORGET: the ids (like K2 or S1) of notebook lines that this log shows to be wrong.\n"
    f"Rules: at most {MAX_NEW_KNOWLEDGE} LEARNED lines and {MAX_NEW_SKILLS} SKILL lines, each at most 40 words. A note must stay useful when the "
    "banana and the box lie somewhere else on the table, so write what you learned about your body, your tools and your own mistakes, "
    "not where things were. When an action failed, write the reason the log shows for it (use the numbers that came back), not only "
    "that it failed. Do not write a reason that the numbers in the log do not show. A rule about how to act must be backed by the log: following it "
    "must have got you closer to what you wanted; if following one of your notebook lines did not, FORGET it. Do not put step numbers or "
    "attempt numbers in a note, and do not describe what the harness or the tools do, only what you should do or avoid. "
    "Do not write again what your notebook already says. "
    "Only write what the log supports. If you learned nothing new, write the single word NOTHING.")


_FORGET_OLD = "FORGET: the ids (like K2 or S1) of notebook lines that this log shows to be wrong.\n"
_FORGET_QUOTE = ('FORGET: K2 "words copied exactly from the log" - one notebook line per FORGET line: its id, then, in quotation marks, the words '
                 'copied exactly from the log of this attempt (a result line, a warning, how it ended or what the person said, not your own [note]) '
                 'that show this notebook line to be wrong. A FORGET line without such a quote is ignored.\n')
assert _FORGET_OLD in REFLECT_FORMAT


def reflect_format(forget_quote=False):
    """The reflection instructions. forget_quote (version 13): a FORGET line needs a quote from the log."""
    return REFLECT_FORMAT.replace(_FORGET_OLD, _FORGET_QUOTE) if forget_quote else REFLECT_FORMAT


# =============================================================================== reading the duck's answer
def clean_note(raw, max_chars=MAX_CHARS_K):
    """One raw line -> (clean text, "") or ("", why it was rejected)."""
    t = (raw or "").replace("**", "").replace("`", "")
    t = " ".join(t.split())
    t = re.sub(r"^(?:[-*•]+|\d+[.)])\s*", "", t).strip(" \"'")
    if not t:
        return "", "empty"
    if t.lower().rstrip(".! ") in ("nothing", "none", "nothing new", "n/a"):
        return "", "nothing"
    if re.search(r"[{}]", t) or re.search(r"<[^>]*>", t):
        return "", "looks like markup, not a note"
    if len(t) > max_chars:
        cut = max(t.rfind(". ", 0, max_chars), t.rfind("; ", 0, max_chars))
        if cut < MIN_CHARS:
            return "", "too long"
        t = t[:cut + 1].strip()
    if len(t) < MIN_CHARS:
        return "", "too short"
    return t, ""


_NOTE_LINE = re.compile(r"^\s*(?:[-*•]+\s*|\d+[.)]\s*|#+\s*)?(LEARNED|LEARNT|KNOWLEDGE|FACT|SKILL)\s*[:\-–]\s*(.*)$", re.I)
_FORGET_LINE = re.compile(r"^\s*(?:[-*•]+\s*|\d+[.)]\s*|#+\s*)?FORGET\b\s*[:\-–]?\s*(.*)$", re.I)
_QUOTE = re.compile(r'"([^"]+)"|\u201c([^\u201d]+)\u201d')                  # a quote in straight or curly double quotation marks


def parse_reflection(text):
    """The duck's answer -> {"knowledge": [raw], "skills": [raw], "forget": [ids], "forget_quotes": {id: quote}, "mode": "lines" | "fallback" | "nothing"}.
    Lines that continue a note (not starting a new one) are joined to it. An answer without any LEARNED / SKILL line has its bullet
    points taken as LEARNED notes ("fallback"). A FORGET line: the ids stand before the first quotation; the quote belongs to the first id."""
    out = {"knowledge": [], "skills": [], "forget": [], "forget_quotes": {}, "mode": "nothing"}
    cur = None                                              # the list item being continued: (list, index)
    loose = []
    for line in (text or "").replace("**", "").splitlines():
        if not line.strip():
            cur = None
            continue
        m = _NOTE_LINE.match(line)
        f = None if m else _FORGET_LINE.match(line)
        if m:
            kind = m.group(1).upper()
            lst = out["skills"] if kind == "SKILL" else out["knowledge"]
            lst.append(m.group(2).strip())
            cur = (lst, len(lst) - 1)
            out["mode"] = "lines"
        elif f:
            body = f.group(1)
            q = _QUOTE.search(body)
            quote = " ".join(((q.group(1) or q.group(2)) if q else "").split())
            ids = [x.upper() for x in re.findall(r"\b([KkSs]\d+)\b", body[:q.start()] if q else body)]
            out["forget"] += ids
            for i, nid in enumerate(ids):
                if i == 0 and quote and not out["forget_quotes"].get(nid):
                    out["forget_quotes"][nid] = quote
                else:
                    out["forget_quotes"].setdefault(nid, "")
            cur = None
            out["mode"] = "lines"
        elif cur is not None:
            cur[0][cur[1]] += " " + line.strip()
        else:
            loose.append(line.strip())
    if out["mode"] != "lines":
        for ln in loose:
            body = re.sub(r"^(?:[-*•]+|\d+[.)])\s*", "", ln).strip()
            bullet = bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s", ln))
            if bullet and len(body) >= 25 and not body.endswith(":"):
                out["knowledge"].append(body)
        if out["knowledge"]:
            out["mode"] = "fallback"
    return out


def _words(text):
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def similar(a, b):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= MERGE_SIMILARITY


def _norm(text):
    t = (text or "").replace("**", "").replace("`", "").replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(t.lower().split())


def quote_in_log(quote, log_text):
    """Version 13: is `quote` really part of the log? Case and spacing are ignored, a leading or closing "..." is ignored, the quote must be at
    least MIN_QUOTE_CHARS long. This is all the code checks: it does not judge whether the quote shows the note to be wrong."""
    q = re.sub(r"^(?:\.\.\.|\u2026)\s*", "", _norm(quote))
    q = re.sub(r"\s*(?:\.\.\.|\u2026)$", "", q)
    return len(q) >= MIN_QUOTE_CHARS and q in _norm(log_text)


def evidence_text(digest, ended="", verdict=""):
    """The part of what the duck read in the reflection that can be quoted as evidence: the log lines WITHOUT the duck's own [note]s (its own
    words are not evidence), the harness warnings, how the attempt ended and what the person said."""
    lines = [re.sub(r"\s*\[note: .*\]\s*$", "", ln) for ln in (digest or "").split("\n")]
    return "\n".join(lines + [ended or "", verdict or ""])


# =============================================================================== the notebook
class Notebook:
    def __init__(self, path=None, fresh=False):
        self.path = path
        self.knowledge, self.skills, self.history = [], [], []
        self.n_k = self.n_s = 0
        self.archived = None
        if path and os.path.exists(path):
            if fresh:
                self.archived = f"{path}.old_{time.strftime('%Y%m%d_%H%M%S')}"
                os.replace(path, self.archived)               # kept, not deleted
            else:
                self.load()

    # ------------------------------------------------------------------ files
    def load(self):
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.knowledge, self.skills, self.history = d.get("knowledge", []), d.get("skills", []), d.get("history", [])
        self.n_k = max([int(x["id"][1:]) for x in self.knowledge] + [d.get("n_k", 0)])
        self.n_s = max([int(x["id"][1:]) for x in self.skills] + [d.get("n_s", 0)])

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.as_dict(), f, indent=2, ensure_ascii=False)
        with open(os.path.splitext(self.path)[0] + ".md", "w", encoding="utf-8") as f:
            f.write(self.markdown())

    def as_dict(self):
        return {"note": "Franky's notebook. Written by the duck itself (the model's weights never change). tries = attempts the note was "
                        "shown in, wins = how many of those succeeded.",
                "n_k": self.n_k, "n_s": self.n_s, "knowledge": self.knowledge, "skills": self.skills, "history": self.history}

    def markdown(self):
        def rows(items):
            return [f"- **{it['id']}** {it['text']}  \n  _(from {it['source']}; seen {it['seen']}x; shown in {it['tries']} attempts, "
                    f"{it['wins']} of them succeeded)_" for it in items] or ["- (nothing yet)"]
        lines = ["# Franky's notebook", "", "## What I learned", *rows(self.knowledge), "", "## Skills I worked out", *rows(self.skills), "",
                 "## Attempts so far", ""]
        lines += [f"- scene {h.get('scene')} attempt {h.get('attempt')}: {'SUCCESS' if h.get('success') else 'failed'}, {h.get('steps')} tool calls, "
                  f"ended: {h.get('ended')}" for h in self.history] or ["- (none)"]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ reading
    def items(self):
        return self.knowledge + self.skills

    def find(self, nid):
        return next((it for it in self.items() if it["id"] == nid), None)

    def shown_ids(self):
        return [it["id"] for it in self.items()]

    def block(self):
        """The text shown to the duck at the start of an attempt ("" while the notebook is empty)."""
        if not self.items():
            return ""
        lines = ["Your notebook. You wrote these notes yourself after earlier attempts. They can be incomplete or wrong, so check them "
                 "against what you see and measure."]
        if self.knowledge:
            lines += ["What you learned:"] + [f"{it['id']}. {it['text']}" for it in self.knowledge]
        if self.skills:
            lines += ["Skills you worked out:"] + [f"{it['id']}. {it['text']}" for it in self.skills]
        return "\n".join(lines)

    # ------------------------------------------------------------------ writing
    def learn(self, parsed, source, log_text=None, fair=False):
        """Take a parsed reflection into the notebook. -> {"added", "merged", "forgotten", "evicted": [items], "rejected": [(raw, why)]}
        log_text (version 13): the evidence the duck may quote (evidence_text); given = a FORGET needs a quote that is in it, None = the old FORGET.
        fair (version 13): a note is only pushed out after MIN_TRIES attempts, the least often written one first; False = the old rule."""
        out = {"added": [], "merged": [], "forgotten": [], "evicted": [], "rejected": []}
        quotes = parsed.get("forget_quotes", {})
        forgets = parsed.get("forget", []) if log_text is not None else parsed.get("forget", [])[:MAX_FORGET]
        for nid in forgets:
            it = self.find(nid)
            quote = quotes.get(nid, "")
            if it is None:
                out["rejected"].append((nid, "no such note"))
            elif log_text is not None and len(out["forgotten"]) >= MAX_FORGET:
                out["rejected"].append((nid, f"kept: at most {MAX_FORGET} notes can be forgotten in one reflection"))
            elif log_text is not None and not quote:
                out["rejected"].append((nid, "kept: a FORGET needs a quote from the log in quotation marks"))
            elif log_text is not None and not quote_in_log(quote, log_text):
                out["rejected"].append((nid, f"kept: the quote is not in the log: {quote[:60]!r}"))
            elif it["wins"] >= PROTECT_WINS:
                out["rejected"].append((nid, f"kept: it was in {it['wins']} successful attempts"))
            else:
                (self.knowledge if nid.startswith("K") else self.skills).remove(it)
                if quote:
                    it["forget_quote"] = quote                                    # on the removed note only: learn.py prints and logs it
                out["forgotten"].append(it)
        for key, prefix, limit_new, max_chars, cap in (("knowledge", "K", MAX_NEW_KNOWLEDGE, MAX_CHARS_K, MAX_KNOWLEDGE),
                                                        ("skills", "S", MAX_NEW_SKILLS, MAX_CHARS_S, MAX_SKILLS)):
            items, taken = getattr(self, key), 0
            for raw in parsed.get(key, []):
                text, why = clean_note(raw, max_chars)
                if not text:
                    if why != "nothing":
                        out["rejected"].append((raw, why))
                    continue
                if taken >= limit_new:
                    out["rejected"].append((text, f"more than {limit_new} notes of this kind in one reflection"))
                    continue
                taken += 1
                dup = next((it for it in items if similar(it["text"], text)), None)
                if dup:
                    dup["seen"] += 1
                    out["merged"].append(dup)
                    continue
                if prefix == "K":
                    self.n_k += 1
                    nid, order = f"K{self.n_k}", self.n_k
                else:
                    self.n_s += 1
                    nid, order = f"S{self.n_s}", self.n_s
                it = {"id": nid, "text": text, "source": source, "seen": 1, "tries": 0, "wins": 0, "order": order,
                      "added": time.strftime("%Y-%m-%d %H:%M:%S")}
                items.append(it)
                out["added"].append(it)
            while len(items) > cap:
                # old rule: the weakest OLD note (wins * 3 + times written) makes room, a new one always gets in.
                # fair rule (version 13): only a note that was shown in MIN_TRIES attempts can be pushed out, the one written down least often first
                # (then the one with fewer wins, then the oldest); if there is none, the newest new note is not added.
                old = [it for it in items if it not in out["added"]]
                if fair:
                    old = [it for it in old if it["tries"] >= MIN_TRIES]
                if not old:
                    mine = [it for it in out["added"] if it in items]
                    if not fair or not mine:
                        break
                    drop = mine[-1]
                    items.remove(drop)
                    out["added"].remove(drop)
                    if drop["order"] == (self.n_k if prefix == "K" else self.n_s):  # give the id back: nobody has seen it
                        if prefix == "K":
                            self.n_k -= 1
                        else:
                            self.n_s -= 1
                    out["rejected"].append((drop["text"], f"not added: the notebook is full and no note has had its {MIN_TRIES} tries yet"))
                    continue
                key = (lambda it: (it["seen"], it["wins"], it["order"])) if fair else (lambda it: (it["wins"] * 3 + it["seen"], it["order"]))
                victim = min(old, key=key)
                items.remove(victim)
                out["evicted"].append(victim)
        self.save()
        return out

    def record_attempt(self, shown, success, info):
        """After an attempt: every note that was shown to the duck has been tried once more (and won, if the attempt succeeded)."""
        for it in self.items():
            if it["id"] in shown:
                it["tries"] += 1
                it["wins"] += 1 if success else 0
        self.history.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "success": bool(success), **info})
        self.save()


# =============================================================================== the log the duck reads
def _short_result(name, text):
    lines = [l.strip() for l in (text or "").split("\n") if l.strip() and not l.startswith("Beak:") and not l.startswith("New picture")]
    if not lines:
        return ""
    head = lines[0]
    if name == "find":
        mid = next((l for l in lines if "middle of the box on the table" in l), "")
        fp = next((l for l in lines if l.startswith("footprint on the table")), "")
        return (head[:80] + (" " + mid.replace("middle of the box on the table: ", "middle on the table: ")[:170] if mid else "")
                + (" | " + fp.replace("footprint on the table: about ", "footprint ")[:90] if fp else ""))[:340]
    if name == "measure":
        keep = [l for l in lines[1:] if l.startswith("spot") or l.startswith("middle between") or l.startswith("line from")]
        return (head + " " + " | ".join(keep))[:300]
    if name in ("close_beak", "pick"):                                   # the beak-sense line says why the banana was not taken: keep it
        sense = next((l for l in lines if l.startswith("Beak sense:")), "")
        return (head.split(". ")[0][:90] + ("." + (" " + sense[:300] if sense else "")))[:400]
    return head[:200]


def attempt_digest(msgs, max_lines=DIGEST_LINES):
    """The conversation of an attempt -> numbered lines: the tool, its arguments, what came back (the numbers), the duck's own note,
    and the warnings the harness gave. Only what the duck itself saw. No pictures.
    A long attempt is shortened like this: the first 4 and the last 8 lines stay, so do all the lines of the tools that decide the attempt
    (close_beak, open_beak, done) and the walks; the looking (look / find / measure) in the middle goes first (version 6: the old rule kept the
    first 8 and the last 21 lines and cut the middle, which is where the grasp was)."""
    items, pending, n = [], None, 0                         # items: (tool name, text, [warning lines that followed it])
    for m in msgs[2:]:
        role, content = m.get("role"), m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            fn = m["tool_calls"][0].get("function", {})
            args = dict(fn.get("arguments") or {})
            pending = (fn.get("name"), args, args.pop("note", None))
        elif role == "tool" and pending:
            n += 1
            name, args, note = pending
            pending = None
            shown = ", ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}" for k, v in args.items())
            items.append([name, f"{n}. {name}({shown}) -> {_short_result(name, content)}" + (f"   [note: {str(note)[:140]}]" if note else ""), []])
        elif role == "user" and content.startswith("[guard]") and items:
            items[-1][2].append("   (warning you got: " + content[len("[guard]"):].strip()[:200] + ")")
        elif role == "user" and content.startswith("[guard]"):
            items.append(["", "", ["   (warning you got: " + content[len("[guard]"):].strip()[:200] + ")"]])

    def count(keep):
        return sum(1 + len(items[i][2]) for i in keep if items[i][1]) + sum(len(items[i][2]) for i in keep if not items[i][1])

    keep = list(range(len(items)))
    if count(keep) > max_lines:
        always = set(range(min(4, len(items)))) | set(range(max(0, len(items) - 8), len(items)))
        always |= {i for i, it in enumerate(items) if it[0] in DECISIVE}
        for kind in (("look", "find", "measure"), ("walk", "turn"), ()):         # drop the looking first, then the walks, then (last) anything
            for i in list(keep):
                if count(keep) <= max_lines:
                    break
                if i not in always and (not kind or items[i][0] in kind):
                    keep.remove(i)
    if count(keep) > max_lines:                                # still too long (many closes of the beak): the oldest of those go too
        base = set(range(min(4, len(items)))) | set(range(max(0, len(items) - 8), len(items)))
        for i in list(keep):
            if count(keep) <= max_lines:
                break
            if i not in base:
                keep.remove(i)
    out, last = [], -1
    for i in keep:
        if i != last + 1 and last >= 0:
            out.append(f"   ... ({i - last - 1} lines left out) ...")
        last = i
        if items[i][1]:
            out.append(items[i][1])
        out += items[i][2]
    return "\n".join(out) if out else "(you did not call any tool)"


def ended_text(res, max_steps):
    """How the attempt ended, in plain words, for the duck (these are facts about the harness, not about the scene)."""
    e = res.get("ended")
    return {
        "done": "You told the person you had finished.",
        "max_steps": f"You used all {max_steps} tool calls and the attempt stopped.",
        "guard_stalled": "The harness stopped the attempt: nothing in the world changed for 10 tool calls in a row.",
        "guard_failures": "The harness stopped the attempt: too many of your moves (walking, closing or opening the beak) did not do what they are for.",
        "guard_budget": "The harness stopped the attempt: it took too long.",
        "guard_loop": "The harness stopped the attempt: you kept repeating a failing action.",
        "fell": "You fell over, so the attempt stopped.",
        "off_table": "You walked off the table, so the attempt stopped.",
        "no_tool_calls": "You stopped calling tools, so the attempt stopped.",
    }.get(e, f"The attempt ended: {e}.")


def verdict_text(duck, res):
    """What a person watching would say after the attempt. This is the only place where the TRUE state reaches the duck (learn.py --feedback off removes it)."""
    from sim.duck_sim import TABLE_H
    if res.get("success"):
        return "The person says: the banana is in the green box. The command is completed."
    pos = duck.banana_pos()
    if pos[2] < TABLE_H - 0.05:
        where = " The banana fell off the table."
    elif duck.holding:
        where = " You are still holding the banana."
    else:
        where = " The banana lies on the table."
    claim = " You said you had finished, but you had not." if res.get("false_claim") else ""
    return "The person says: the banana is NOT in the green box." + where + claim


REFLECT_FACTS_HEAD = "\n\nWhat you were told about your camera and your body before the attempt (all of it was true during the attempt):\n"


def reflection_messages(command, notebook_block, digest, ended, verdict, note="", facts="", forget_quote=False):
    """The two messages of the reflection call. `facts` (version 11): the camera and body facts the duck had while it acted (franky_tools.body_facts).
    `forget_quote` (version 13): a FORGET line must carry a quote from the log."""
    user = (f"Command you were given: {command}\n\n"
            + (notebook_block + "\n\n" if notebook_block else "Your notebook is still empty.\n\n")
            + "Log of your last attempt (each line: its step number in this attempt, the tool you called with its arguments -> what came back; [note] is what you wrote down at that moment):\n"
            + digest + f"\n\nHow it ended: {ended}\n" + (verdict + "\n" if verdict else "") + (note + "\n" if note else "") + "\n" + reflect_format(forget_quote))
    return [{"role": "system", "content": REFLECT_SYSTEM + (REFLECT_FACTS_HEAD + facts.strip() if facts and facts.strip() else "")}, {"role": "user", "content": user}]
