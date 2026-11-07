"""guardrails.py - Harness v1, part A3: the guardrails.

The tracker (state_tracker.py) SEES what happens. The guardrails ACT on it - in plain code, the LLM cannot talk them away.
Everything here is switched by --guard on|off in harness.py, so "with guardrails" and "without" can be measured.

  robot safety  (GuardedTools, runs BEFORE the motors move)
      table_edge     refuse to walk to a target closer than 10 cm to the table edge
      done_rejected  a "done" while the banana is NOT in the box is sent back (at most 2 times)
  agent behaviour (Guard, runs AFTER every tool call)
      fresh_look     the harness itself looks again and shows the result as a fake `observe` call
                     (used when the scene changed by itself, and as a warning when Franky is looping)
      guard_loop     the loop flag came up twice in a row -> stop the episode
      guard_failures too many missed goals in one episode -> stop
      guard_budget   too much SIMULATED time used -> stop
  always on (not switched by --guard)
      chat_with_retry  one retry when the LLM call fails, then the episode ends with `llm_error` instead of a crash
"""

import time
import urllib.error

from sim.duck_sim import TABLE_H, TABLE_HALF
from agent_harness.franky_agent import FrankyTools

EDGE_MARGIN = 0.10        # m  : never walk to a target closer than this to the table edge
MAX_DONE_REJECTS = 2      # a false "done" is sent back at most this many times per episode
MAX_FAILED_GOALS = 5      # stop the episode after this many missed goals
MAX_SIM_S = 90.0          # stop the episode after this much simulated time (a normal run needs ~17-25 s)
CTX_WARN = 0.85           # warn when the prompt fills this share of the context window


# =============================================================================== the LLM call
class LLMError(RuntimeError):
    pass


def chat_with_retry(brain, msgs, tools, tries=2, wait=2.0):
    """brain.chat(), but a failed call is tried once more; after that LLMError (the episode ends, the run goes on)."""
    last = None
    for k in range(tries):
        try:
            return brain.chat(msgs, tools)
        except (OSError, RuntimeError, ValueError) as e:      # URLError and timeouts are OSErrors, bad JSON is a ValueError
            last = e
            if k + 1 < tries:
                time.sleep(wait)
    raise LLMError(str(last))


# =============================================================================== robot safety: before the motors move
class GuardedTools(FrankyTools):
    """FrankyTools + safety checks. With on=False it behaves exactly like FrankyTools."""

    def __init__(self, duck, on=True):
        super().__init__(duck)
        self.on, self.triggers, self.done_rejected = on, [], 0

    def _trigger(self, name, detail):
        self.triggers.append({"guard": name, "detail": detail})

    def walk_to(self, target=None):
        if self.on and target in ("banana", "green_box"):
            b = self.d.banana_pos()
            xy = b[:2] if target == "banana" else self._drop_spot()
            on_table = target != "banana" or b[2] > TABLE_H - 0.05      # a banana that fell off is handled by the tool
            if on_table and (abs(xy[0]) > TABLE_HALF[0] - EDGE_MARGIN or abs(xy[1]) > TABLE_HALF[1] - EDGE_MARGIN):
                self._trigger("table_edge", f"{target} at ({xy[0]:.2f}, {xy[1]:.2f})")
                return f"Refused for safety: the {target} is too close to the table edge, you could fall off."
        return super().walk_to(target)

    def done(self):
        if self.on and not self.d.banana_in_box() and self.done_rejected < MAX_DONE_REJECTS:
            self.done_rejected += 1
            self._trigger("done_rejected", f"attempt {self.done_rejected}")
            return "Not finished: the banana is not in the green box yet. Observe, then continue."
        return super().done()


# =============================================================================== agent behaviour: after every tool call
def look_exchange(note, tools):
    """A fake `observe` tool call + its result, so the LLM reads the fresh state in the channel it trusts most."""
    return [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "observe", "arguments": {}}}]},
            {"role": "tool", "tool_name": "observe", "content": f"[guard] {note}\n" + tools.observe()}]


class Guard:
    """Reads the tracker's verdict after each tool call and decides: carry on, look again, or stop."""

    def __init__(self, on=True, max_failed=MAX_FAILED_GOALS, max_sim_s=MAX_SIM_S):
        self.on, self.max_failed, self.max_sim_s = on, max_failed, max_sim_s
        self.hits = 0                                   # how many calls in a row carried a loop flag

    def after_call(self, v, failed_total, sim_s):
        """-> ("continue", None) | ("look", warning text) | ("abort", reason)"""
        if not self.on:
            return "continue", None
        self.hits = self.hits + 1 if v["loop"] else 0
        if self.hits >= 2:
            return "abort", "guard_loop"
        if failed_total >= self.max_failed:
            return "abort", "guard_failures"
        if sim_s >= self.max_sim_s:
            return "abort", "guard_budget"
        if self.hits == 1:
            if v["loop"] == "repeat_fail":
                return "look", (f"WARNING: {v['call']} has already failed {v['repeat']} times and nothing improved. "
                                "Do not repeat it. Try a different action.")
            return "look", "WARNING: nothing has changed during the last 3 actions. Try a different action."
        return "continue", None