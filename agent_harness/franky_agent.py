"""franky_agent.py - Franky's LLM brain (Step 3).

The LLM never touches the motors. It only sees TEXT and calls five tools, one at a time:

    observe()           look again: where are the banana and the green box?
    walk_to(target)     walk until the target is within reach of the beak   ("banana" / "green_box")
    pick()              crouch and grab the banana  (needs: hands empty + banana IN REACH)
    place()             crouch and let go of the banana (needs: holding it + green box IN REACH)
    done()              "I am finished"  (the harness then checks the REAL result)

Every tool is a skill from duck_sim.py that already works without any LLM.
HONEST NOTES for any write-up:
  * perception is SIMULATED: positions come from the simulator, there is no camera image.
  * the grasp is SIMULATED (the official model has no jaw motor), see duck_sim.py.

    python franky_agent.py                      # 1 episode (seed 0) with gemma4:e2b, no window
    python franky_agent.py --seeds 0 1 2 3 4    # several episodes
    python franky_agent.py --brain rules        # NO LLM: a tiny rule-follower, to test the loop itself
    python franky_agent.py --view               # also open the MuJoCo window
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request

from sim.duck_sim import Duck, TABLE_H, ViewerClosed
from project_paths import log_dir

REACH_R = 0.035    # the beak lands ~0.10 m ahead of the trunk; within 3.5 cm of that spot = "in reach"

SYSTEM = ("You are Franky, a small duck robot standing on a table. Your job: pick up the banana and put it "
          "into the green box. You act ONLY by calling one tool at a time, and after each call you see the result. "
          "Look at what you see, then call the single next tool that makes progress. If a tool fails, look again "
          "(observe) and try something else. After you place the banana, observe to check where it landed. "
          "Call done only when you see 'banana: in the green_box'.")

TOOLS = [
    {"type": "function", "function": {
        "name": "walk_to",
        "description": "Walk toward an object until it is within reach of the beak. Use it when the object you "
                       "need is NOT yet in reach.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["banana", "green_box"]}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "pick",
        "description": "Crouch and grab the banana with the beak. Only works when you hold nothing and the "
                       "banana is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "place",
        "description": "Crouch and let go of the banana you are holding. Only works when you hold the banana "
                       "and the green box is IN REACH.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "observe", "description": "Look again and report where things are.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "done", "description": "Say you are finished. Only call it when observe shows 'banana: in the green_box'.",
        "parameters": {"type": "object", "properties": {}}}},
]


# =============================================================================== the body's tools
class FrankyTools:
    def __init__(self, duck: Duck):
        self.d = duck
        self.finished = False

    # --- simulated perception: where is something relative to the duck? ---------------------------
    def _rel(self, xy):
        r = self.d.reach(xy)
        return r["ahead"], r["left"]

    def _drop_spot(self):
        """Where the beak must end up to drop the banana into the tray: 3.5 cm before the tray centre
        (so the feet stay off the tray) - the same trick the scripted baseline uses."""
        bx, by = self.d.box_pos()[:2]
        x, y = self.d.pose()[:2]
        n = math.hypot(bx - x, by - y) or 1.0
        return (bx - 0.035 * (bx - x) / n, by - 0.035 * (by - y) / n)

    @staticmethod
    def _in_reach(ahead, left):
        return math.hypot(ahead - 0.10, left) <= REACH_R

    @staticmethod
    def _fmt(ahead, left, reach):
        fwd = f"{ahead:.2f} m ahead" if ahead >= 0 else f"{-ahead:.2f} m behind"
        side = f"{abs(left):.2f} m {'left' if left >= 0 else 'right'}"
        return f"{fwd}, {side} -> {'IN REACH' if reach else 'NOT in reach'}"

    def _banana_line(self):
        d = self.d
        b = d.banana_pos()
        if d.banana_in_box():
            return "banana: in the green_box."
        if b[2] < TABLE_H - 0.05:
            return "banana: fell off the table."
        a, l = self._rel(b[:2])
        return "banana: " + self._fmt(a, l, self._in_reach(a, l))

    def _box_line(self):
        a, l = self._rel(self.d.box_pos()[:2])
        ra, rl = self._rel(self._drop_spot())
        return "green_box: " + self._fmt(a, l, self._in_reach(ra, rl))

    # --- the tools --------------------------------------------------------------------------------
    def observe(self):
        d = self.d
        if d.is_fallen():
            return "You have fallen over and cannot move."
        if d.off_table():
            return "You walked off the table."
        lines = [f"You hold: {'banana' if d.holding else 'nothing'}."]
        if not d.holding:
            lines.append(self._banana_line())
        lines.append(self._box_line())
        return "\n".join(lines)

    def walk_to(self, target=None):
        d = self.d
        if target not in ("banana", "green_box"):
            return "Unknown target. Use target = banana or green_box."
        if target == "banana":
            if d.holding:
                return "You are already holding the banana."
            if d.banana_in_box():
                return "The banana is already in the green box."
            if d.banana_pos()[2] < TABLE_H - 0.05:
                return "Cannot walk to the banana: it fell off the table."
            xy = d.banana_pos()[:2]
        else:
            xy = self._drop_spot()
        r = d.walk_to(xy)
        d.settle()
        if d.is_fallen():
            return "Failed: you fell over."
        if d.off_table():
            return "Failed: you walked off the table."
        return f"Walked toward {target} ({r['seconds']} s). " + (self._banana_line() if target == "banana" else self._box_line())

    def pick(self):
        d = self.d
        if d.holding:
            return "Cannot pick: you already hold the banana."
        if d.banana_in_box():
            return "Cannot pick: the banana is already in the green box."
        a, l = self._rel(d.banana_pos()[:2])
        if not self._in_reach(a, l):
            return "Cannot pick: " + self._banana_line() + " Use walk_to banana first."
        p = d.ground_pick(grasp=True)
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        if p["grasped"]:
            return "Picked: the banana is in your beak."
        return "Missed: the beak closed on nothing, the banana is still on the table. Observe and try again."

    def place(self):
        d = self.d
        if not d.holding:
            return "Cannot place: you are not holding the banana."
        ra, rl = self._rel(self._drop_spot())
        if not self._in_reach(ra, rl):
            return "Cannot place: " + self._box_line() + " Use walk_to green_box first."
        d.ground_pick(release=True)
        d.hold(1.5)                                  # let the banana settle
        if d.is_fallen():
            return "Failed: you fell over while crouching."
        return "Released the banana over the green box. Observe to check where it landed."

    def done(self):
        self.finished = True
        return "Finished."

    def call(self, name, args):
        fn = getattr(self, name, None) if name in ("observe", "walk_to", "pick", "place", "done") else None
        if fn is None:
            return f"Unknown tool '{name}'. Tools: walk_to, pick, place, observe, done."
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"





# =============================================================================== the brains
class OllamaBrain:
    """The real thing: a local model through Ollama (http://localhost:11434)."""

    def __init__(self, model="gemma4:e2b", host="http://localhost:11434", num_ctx=4096):
        self.model, self.host, self.num_ctx = model, host.rstrip("/"), num_ctx

    def _post(self, body, timeout=900):
        req = urllib.request.Request(self.host + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def chat(self, messages, tools=None, num_predict=300):
        body = {"model": self.model, "messages": messages, "stream": False, "think": False, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": num_predict}}
        if tools:
            body["tools"] = tools
        try:
            return self._post(body)
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if "think" in err.lower():               # model has no thinking switch -> retry without it
                del body["think"]
                return self._post(body)
            raise RuntimeError(err.strip() or f"HTTP {e.code}")

    def warmup(self):
        t0 = time.time()
        self.chat([{"role": "user", "content": "Say OK."}], num_predict=5)
        return time.time() - t0


class RuleBrain:
    """NO LLM. A few if-statements that read the same text an LLM would read. Only used to test that the
    loop, the tools and the logging work (and as a sanity check of what 'a perfect brain' scores)."""

    model = "rules"

    def warmup(self):
        return 0.0

    def chat(self, messages, tools=None, **kw):
        last = messages[-1]["content"]

        def call(name, **args):
            return {"message": {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                    "prompt_eval_count": 0, "eval_count": 0}

        def line(prefix):
            for ln in last.split("\n"):
                if ln.strip().startswith(prefix):
                    return ln
            return ""

        if last.startswith("Walked toward banana"):
            return call("pick") if "-> IN REACH" in last else call("walk_to", target="banana")
        if last.startswith("Walked toward green_box"):
            return call("place") if "-> IN REACH" in last else call("walk_to", target="green_box")
        if last.startswith("Picked"):
            return call("walk_to", target="green_box")
        if last.startswith("Released"):
            return call("observe")
        if "You hold:" in last:
            if "banana: in the green_box" in last:
                return call("done")
            if "You hold: banana" in last:
                return call("place") if "-> IN REACH" in line("green_box") else call("walk_to", target="green_box")
            return call("pick") if "-> IN REACH" in line("banana") else call("walk_to", target="banana")
        return call("observe")





# =============================================================================== one episode
def run_episode(duck, brain, seed, max_steps=14, verbose=True):
    duck.reset(seed=seed)
    tools = FrankyTools(duck)
    sim0, wall0 = duck.t, time.time()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Task: put the banana into the green box.\nYou look around:\n" + tools.observe()}]
    steps, llm_s, bad, reason = [], 0.0, 0, "max_steps"
    for i in range(1, max_steps + 1):
        t0 = time.time()
        resp = brain.chat(msgs, TOOLS)
        dt = time.time() - t0
        llm_s += dt
        m = resp.get("message", {})
        calls = m.get("tool_calls") or []
        if not calls:                                     # the model talked instead of calling a tool
            bad += 1
            steps.append({"i": i, "llm_s": round(dt, 2), "tool": None, "text": (m.get("content") or "")[:200]})
            if verbose:
                print(f"  step {i:2d} [{dt:4.1f}s]  (no tool call) {(m.get('content') or '')[:80]!r}")
            if bad >= 3:
                reason = "no_tool_calls"
                break
            msgs.append({"role": "assistant", "content": m.get("content") or ""})
            msgs.append({"role": "user", "content": "Reply by calling exactly one tool."})
            continue
        fn = calls[0].get("function", {})
        name, args = fn.get("name"), fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        s0 = duck.t
        result = tools.call(name, args)
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [calls[0]]})
        msgs.append({"role": "tool", "tool_name": name, "content": result})
        steps.append({"i": i, "llm_s": round(dt, 2), "tool": name, "args": args, "result": result,
                      "sim_s": round(duck.t - s0, 1), "prompt_tokens": resp.get("prompt_eval_count"),
                      "gen_tokens": resp.get("eval_count")})
        if verbose:
            shown = ", ".join(f"{k}={v}" for k, v in args.items())
            print(f"  step {i:2d} [{dt:4.1f}s]  {name}({shown})  ->  {result.splitlines()[0][:100]}")
        if tools.finished:
            reason = "done"
            break
        if duck.is_fallen() or duck.off_table():
            reason = "fell" if duck.is_fallen() else "off_table"
            break
    truth = duck.banana_in_box()
    return {"seed": seed, "success": bool(truth), "claimed_done": tools.finished, "reason": reason,
            "steps": len(steps), "llm_s": round(llm_s, 1), "sim_s": round(duck.t - sim0, 1),
            "wall_s": round(time.time() - wall0, 1), "log": steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:e2b")
    ap.add_argument("--brain", choices=["ollama", "rules"], default="ollama")
    ap.add_argument("--seeds", type=int, nargs="*", default=[0])
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--view", action="store_true", help="also open the MuJoCo window (real time)")
    ap.add_argument("--logdir", default=log_dir("harness_v1"))
    args = ap.parse_args()

    brain = RuleBrain() if args.brain == "rules" else OllamaBrain(args.model, args.host)
    if args.brain == "ollama":
        print(f"Loading {args.model} (the very first call can take a minute)...", flush=True)
        try:
            print(f"  ready after {brain.warmup():.0f} s", flush=True)
        except Exception as e:
            raise SystemExit(f"Cannot talk to Ollama / the model: {e}\nIs the Ollama app running? Did you `ollama pull {args.model}`?")

    duck = Duck("Franky", viewer=args.view, realtime=args.view)
    os.makedirs(args.logdir, exist_ok=True)
    logpath = os.path.join(args.logdir, f"franky_{brain.model.replace(':', '-')}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    results = []
    try:
        for seed in args.seeds:
            print(f"\n=== episode seed {seed}  (brain: {brain.model}) ===")
            r = run_episode(duck, brain, seed, args.max_steps)
            results.append(r)
            with open(logpath, "a", encoding="utf-8") as f:
                f.write(json.dumps({**r, "model": brain.model}) + "\n")
            claim = "said done" if r["claimed_done"] else "did NOT say done"
            print(f"  RESULT: banana in box = {r['success']}   ({claim}; ended: {r['reason']}; {r['steps']} tool calls, "
                  f"LLM {r['llm_s']} s, sim {r['sim_s']} s, wall {r['wall_s']} s)")
    except ViewerClosed:
        print("\nwindow closed - stopped.")
    if results:
        n, w = len(results), sum(r["success"] for r in results)
        print(f"\nSUCCESS {w}/{n}   mean tool calls {sum(r['steps'] for r in results)/n:.1f}   "
              f"mean LLM time {sum(r['llm_s'] for r in results)/n:.1f} s   mean sim time {sum(r['sim_s'] for r in results)/n:.1f} s")
        print(f"log saved: {logpath}")
    duck.close()


if __name__ == "__main__":
    main()
    