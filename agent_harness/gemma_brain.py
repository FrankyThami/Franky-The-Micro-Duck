"""gemma_brain.py - the LLM client for autonomous Franky: Gemma 4 (or any vision + tool-calling model) through Ollama.

What it adds to the old OllamaBrain (franky_agent.py, which duck2 still uses and which is left untouched):
  * PICTURES: a message may carry `images` (base64 JPEGs). The model sees the real camera picture.
  * THINKING: `think=True` lets Gemma 4 reason before it acts (its reasoning is returned in message["thinking"] and logged;
    it is NOT sent back in the history, the `note` the model writes in its tool calls is what carries the plan forward).
  * CONTEXT MANAGEMENT: pictures are big. Only the latest `keep_images` pictures are sent; older ones are replaced by a
    short text. When the prompt gets close to the context window, the oldest steps are replaced by a one-line-per-step
    summary (the tool, its arguments, the first line of its result and the model's own note). Nothing is judged or added.
  * a retry without thinking when the model used up its whole answer budget on thinking and called no tool.

It decides nothing about the task. The messages it receives are the whole conversation; it only chooses what to send.
"""

import base64
import json
import time
import urllib.error
import urllib.request

KEEP = ("role", "content", "images", "tool_calls", "tool_name")        # the only message fields Ollama gets

# the question behind the `find` tool: Gemma 4's own detection mode (JSON boxes, 0-1000). {order} is "y1, x1, y2, x2" (its native order) or "x1, y1, x2, y2".
GROUND_PROMPT = ('Detect every {what} in the image. Reply with JSON only: a list of objects like '
                 '{{"box_2d": [{order}], "label": "..."}}, with coordinates normalised to 0-1000. If there is none, reply [].')
GROUND_ORDERS = {"yxyx": "y1, x1, y2, x2", "xyxy": "x1, y1, x2, y2"}


class GemmaBrain:
    def __init__(self, model="gemma4:e2b", host="http://localhost:11434", num_ctx=8192, think=True, num_predict=2048,
                 temperature=0.3, top_p=0.95, top_k=64, seed=0, keep_images=1, max_groups=40):
        self.model, self.host, self.num_ctx = model, host.rstrip("/"), int(num_ctx)
        self.think, self.num_predict = bool(think), int(num_predict)
        self.temperature, self.top_p, self.top_k, self.seed = temperature, top_p, top_k, seed
        self.keep_images, self.max_groups = int(keep_images), int(max_groups)
        self.new_episode()

    # ------------------------------------------------------------------ bookkeeping
    def new_episode(self):
        self._dropped = 0                 # how many old steps are replaced by the summary
        self._last_prompt = 0             # prompt tokens of the previous call (Ollama tells us)

    def describe(self):
        """The settings, for run_meta.json."""
        return {"model": self.model, "num_ctx": self.num_ctx, "think": self.think, "num_predict": self.num_predict,
                "temperature": self.temperature, "top_p": self.top_p, "top_k": self.top_k, "seed": self.seed,
                "keep_images": self.keep_images, "max_groups": self.max_groups}

    # ------------------------------------------------------------------ talking to Ollama
    def _post(self, path, body, timeout=900):
        req = urllib.request.Request(self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def capabilities(self):
        """What Ollama says the model can do: ['completion', 'vision', 'tools', 'thinking', ...] (None when unknown)."""
        try:
            return self._post("/api/show", {"model": self.model}, timeout=30).get("capabilities")
        except Exception:
            return None

    def check(self):
        """Fail early, with a clear message, if the model cannot see pictures or call tools. -> list of warnings."""
        caps, warnings = self.capabilities(), []
        if caps is None:
            return ["could not read the model's capabilities from Ollama (/api/show); continuing"]
        if "vision" not in caps:
            raise RuntimeError(f"{self.model} cannot read pictures (Ollama capabilities: {caps}). Franky needs a vision model, e.g. gemma4:e2b.")
        if "tools" not in caps:
            raise RuntimeError(f"{self.model} cannot call tools (Ollama capabilities: {caps}).")
        if self.think and "thinking" not in caps:
            self.think = False
            warnings.append(f"{self.model} has no thinking mode (capabilities: {caps}); running with --think off")
        return warnings

    def _request(self, messages, tools, think, opts=None):
        body = {"model": self.model, "messages": messages, "stream": False, "keep_alive": "30m",
                "options": {"temperature": self.temperature, "top_p": self.top_p, "top_k": self.top_k, "seed": self.seed,
                            "num_ctx": self.num_ctx, "num_predict": self.num_predict}}
        body["options"].update(opts or {})
        body["think"] = bool(think)
        if tools:
            body["tools"] = tools
        try:
            return self._post("/api/chat", body)
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if "think" in err.lower() and "think" in body:             # the model has no thinking switch -> retry without it
                self.think = False
                del body["think"]
                return self._post("/api/chat", body)
            raise RuntimeError(err.strip() or f"HTTP {e.code}")

    def ground(self, jpeg, what, order="yxyx"):
        """The `find` tool's vision call: one picture + "detect every <what>" in Gemma's native box format, no thinking, no tools,
        temperature 0. -> (the model's answer text, seconds). The conversation of the agent is not touched."""
        msg = {"role": "user", "content": GROUND_PROMPT.format(what=what, order=GROUND_ORDERS.get(order, GROUND_ORDERS["yxyx"])),
               "images": [base64.b64encode(jpeg).decode()]}                 # picture first in the message, as Gemma 4 prefers
        t0 = time.time()
        resp = self._request([msg], None, False, opts={"temperature": 0.0, "num_predict": 400})
        return (resp.get("message", {}).get("content") or "").strip(), time.time() - t0

    def complete(self, messages, think=None, num_predict=1500):
        """A plain text answer: no tools, no pictures (learn.py uses it for the duck's reflection on an attempt).
        -> (answer text, thinking text, seconds). If the model spent its whole budget on thinking and wrote no answer, it is asked once more without thinking."""
        t0 = time.time()
        think = self.think if think is None else bool(think)
        msgs = [{"role": m["role"], "content": m.get("content") or ""} for m in messages]
        opts = {"num_predict": int(num_predict)}
        resp = self._request(msgs, None, think, opts=opts)
        m = resp.get("message", {})
        text, thinking = (m.get("content") or "").strip(), m.get("thinking") or ""
        if think and not text:                                    # it thought until the budget ran out: ask again without thinking
            resp = self._request(msgs, None, False, opts=opts)
            text = (resp.get("message", {}).get("content") or "").strip()
        return text, thinking, time.time() - t0

    def warmup(self):
        t0 = time.time()
        self._request([{"role": "user", "content": "Say OK."}], None, False)
        return time.time() - t0

    # ------------------------------------------------------------------ what is sent
    def _summary(self, groups):
        """One line per dropped step - plain facts from the conversation, nothing added."""
        lines = []
        for g in groups:
            call = next((m for m in g if m["role"] == "assistant"), None)
            res = next((m for m in g if m["role"] == "tool"), None)
            if call is None:
                continue
            tc = (call.get("tool_calls") or [{}])[0].get("function", {})
            args = dict(tc.get("arguments") or {})
            note = args.pop("note", None)
            head = f"{tc.get('name', '?')}({', '.join(f'{k}={v}' for k, v in args.items())})" if tc else "(no tool call)"
            line = f"- {head}"
            if res:
                line += " -> " + (res.get("content") or "").split("\n")[0][:170]
            if note:
                line += f"   [your note: {str(note)[:200]}]"
            lines.append(line)
        return {"role": "user", "content": "Earlier steps (older details were dropped to save memory):\n" + "\n".join(lines)}

    def prepare(self, messages):
        """Full conversation -> (messages to send, info). Never changes `messages`."""
        head, rest = list(messages[:2]), messages[2:]
        groups, cur = [], []
        for m in rest:                                                  # one group = an assistant message + what followed it
            if m["role"] == "assistant" and cur:
                groups.append(cur)
                cur = []
            cur.append(m)
        if cur:
            groups.append(cur)
        # --- 1. when the prompt is nearly full (or the history is long), the oldest steps are replaced by the summary
        room = self.num_ctx - self.num_predict - 400
        if self._last_prompt and self._last_prompt > room and len(groups) - self._dropped > 4:
            self._dropped += max(2, (len(groups) - self._dropped) // 3)
        if len(groups) - self._dropped > self.max_groups:
            self._dropped = len(groups) - self.max_groups
        kept = groups[self._dropped:]
        out = head + ([self._summary(groups[:self._dropped])] if self._dropped else [])
        out += [m for g in kept for m in g]
        # --- 2. only the latest pictures are sent
        clean, n_img, removed = [], 0, 0
        for m in reversed(out):
            c = {k: m[k] for k in KEEP if k in m}
            if c.get("images"):
                if n_img < self.keep_images:
                    n_img += 1
                else:
                    c.pop("images")
                    c["content"] = (c.get("content") or "") + " (the picture was removed to save memory)"
                    removed += 1
            clean.append(c)
        clean.reverse()
        return clean, {"sent_messages": len(clean), "dropped_steps": self._dropped, "pictures_sent": n_img, "pictures_removed": removed}

    def chat(self, messages, tools=None, **kw):
        msgs, info = self.prepare(messages)
        resp = self._request(msgs, tools, self.think)
        m = resp.get("message", {})
        if self.think and not (m.get("tool_calls") or []) and resp.get("done_reason") == "length":
            resp = self._request(msgs, tools, False)                    # it thought until the budget ran out: ask again, no thinking
            info["retry_without_thinking"] = True
        self._last_prompt = resp.get("prompt_eval_count") or self._last_prompt
        resp["_ctx"] = info
        return resp
