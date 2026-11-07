"""llm_speed_test.py - which local model should be Franky's brain?   (Step 3a)

For every model it measures, on YOUR laptop (RTX 3050 4 GB):
  * how much of the model sits on the GPU (100% = fast; less = spills to the CPU = slow)
  * load time, and tokens per second while it writes
  * a "decision" test: Franky is told what he can see and must pick ONE tool.
    6 scenes, we check if the model picked the right tool. Time per decision is what the
    agent loop will feel like (observe -> think -> act).

Needs only Ollama running (it starts with Windows). No extra Python packages.

    python llm_speed_test.py                              # the 3 candidates
    python llm_speed_test.py qwen3-vl:2b gemma4:e2b       # your own list
    python llm_speed_test.py --runs 3                     # repeat the 6 scenes 3x (steadier numbers)
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_MODELS = ["qwen3-vl:2b", "gemma4:e2b", "gemma3:4b"]

SYSTEM = ("You are Franky, a small duck robot standing on a table. Your job: pick up the banana and put it "
          "into the green box. You act ONLY by calling one tool at a time. Look at what you see, then call "
          "the single next tool that makes progress.")

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
]

# (what Franky sees, expected tool, expected target or None)   -- same format the real observe() tool will use
SCENES = [
    ("You hold: nothing.\nbanana: 0.32 m ahead, 0.05 m left -> NOT in reach\ngreen_box: 0.30 m ahead, 0.20 m right -> NOT in reach",
     "walk_to", "banana"),
    ("You hold: banana.\ngreen_box: 0.28 m ahead, 0.10 m left -> NOT in reach",
     "walk_to", "green_box"),
    ("You hold: banana.\ngreen_box: 0.10 m ahead, 0.00 m left -> IN REACH",
     "place", None),
    ("You hold: nothing.\nbanana: 0.10 m ahead, 0.01 m left -> IN REACH\ngreen_box: 0.35 m ahead, 0.15 m right -> NOT in reach",
     "pick", None),
    ("You hold: nothing.\nbanana: 0.11 m ahead, 0.02 m right -> IN REACH\ngreen_box: 0.40 m ahead, 0.05 m left -> NOT in reach",
     "pick", None),
    ("You hold: nothing.\nbanana: 0.55 m ahead, 0.30 m left -> NOT in reach\ngreen_box: 0.20 m ahead, 0.10 m right -> NOT in reach",
     "walk_to", "banana"),
]


class Ollama:
    def __init__(self, host):
        self.host = host.rstrip("/")

    def call(self, path, body=None, timeout=600):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.host + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def chat(self, model, messages, tools=None, fmt=None, num_predict=800):
        """One non-streaming chat call. Switches thinking OFF when the model allows it (thinking = slow)."""
        body = {"model": model, "messages": messages, "stream": False, "think": False,
                "options": {"temperature": 0, "num_predict": num_predict}}
        if tools:
            body["tools"] = tools
        if fmt:
            body["format"] = fmt
        try:
            return self.call("/api/chat", body)
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if "think" in err.lower():                    # this model has no thinking switch -> retry without
                del body["think"]
                return self.call("/api/chat", body)
            raise RuntimeError(err.strip() or f"HTTP {e.code}")


def tokens_per_s(resp):
    n, ns = resp.get("eval_count", 0), resp.get("eval_duration", 0)
    return n / (ns / 1e9) if ns else 0.0


def pick_tool(resp, native):
    """-> (tool name, target) the model chose, or (None, None)."""
    msg = resp.get("message", {})
    try:
        if native and msg.get("tool_calls"):
            call = msg["tool_calls"][0]["function"]
            args = call.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            return call["name"], args.get("target")
        text = msg.get("content", "")
        out = json.loads(text[text.index("{"):text.rindex("}") + 1])   # JSON anywhere in the text
        args = out.get("args", {}) or {}
        return out.get("tool"), args.get("target")
    except Exception:
        return None, None


def why_none(resp):
    """One line that shows what the model did instead of calling a tool."""
    msg = resp.get("message", {})
    clip = lambda t: " ".join((t or "").split())[:110]
    return (f"[{resp.get('eval_count', '?')} tokens, stop={resp.get('done_reason', '?')}, "
            f"thinking={len(msg.get('thinking') or '')} chars] text: \"{clip(msg.get('content'))}\"")


def json_prompt():
    lines = []
    for t in TOOLS:
        f = t["function"]
        extra = ' args {"target": "banana" or "green_box"}' if f["name"] == "walk_to" else " (no args)"
        lines.append(f"- {f['name']}{extra}: {f['description']}")
    return (SYSTEM + "\nTools:\n" + "\n".join(lines) +
            "\nReply with ONLY a JSON object like {\"tool\": \"walk_to\", \"args\": {\"target\": \"banana\"}}.")


def test_model(ol, model, runs):
    res = dict(model=model, ok=False)
    tags = [m["name"] for m in ol.call("/api/tags").get("models", [])]
    if model not in tags and (":" in model or model + ":latest" not in tags):
        res["note"] = f"not installed -> run:  ollama pull {model}"
        return res

    # --- load + speed -------------------------------------------------------------------------
    t0 = time.time()
    try:
        r = ol.chat(model, [{"role": "user", "content": "Describe in three sentences how a duck walks."}], num_predict=120)
    except Exception as e:
        res["note"] = f"failed: {str(e)[:120]}"
        return res
    res["load_s"] = r.get("load_duration", 0) / 1e9
    res["first_call_s"] = time.time() - t0
    res["tok_s"] = tokens_per_s(r)
    try:
        ps = [m for m in ol.call("/api/ps").get("models", []) if m.get("name", "").startswith(model.split(":")[0])]
        if ps and ps[0].get("size"):
            res["gpu_pct"] = 100.0 * ps[0].get("size_vram", 0) / ps[0]["size"]
    except Exception:
        pass

    # --- decisions: native tool calling if the model has it, otherwise JSON text ---------------
    native = True
    lat, right, details = [], 0, []
    for rep in range(runs):
        for scene, want_tool, want_target in SCENES:
            msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scene}]
            t1 = time.time()
            try:
                if native:
                    r = ol.chat(model, msgs, tools=TOOLS)
                else:
                    r = ol.chat(model, [{"role": "system", "content": json_prompt()}, msgs[1]], fmt="json")
            except RuntimeError as e:
                if native and "tool" in str(e).lower():     # no native tool support -> switch to JSON text
                    native = False
                    r = ol.chat(model, [{"role": "system", "content": json_prompt()}, msgs[1]], fmt="json")
                else:
                    res["note"] = f"failed: {str(e)[:120]}"
                    return res
            dt = time.time() - t1
            tool, target = pick_tool(r, native)
            good = tool == want_tool and (want_target is None or (target or "").lower().replace(" ", "_") == want_target)
            right += good
            lat.append(dt)
            details.append((want_tool, tool, target, round(dt, 1), good, '' if good else why_none(r)))
    res.update(ok=True, native=native, latency_s=statistics.median(lat), worst_s=max(lat),
               right=right, total=len(details), details=details)
    return res


def gpu(r, unknown='?'):
    return f"{r['gpu_pct']:.0f}%" if 'gpu_pct' in r else unknown


def unload(ol, model):
    try:
        ol.call("/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--runs", type=int, default=1, help="repeat the 6 scenes this many times")
    ap.add_argument("--host", default="http://localhost:11434")
    args = ap.parse_args()
    ol = Ollama(args.host)
    try:
        ol.call("/api/tags", timeout=5)
    except Exception as e:
        sys.exit(f"Cannot reach Ollama at {args.host} ({e}).\nStart the Ollama app (or run `ollama serve`) and try again.")

    results = []
    for m in args.models:
        print(f"\n=== {m} ===", flush=True)
        r = test_model(ol, m, args.runs)
        results.append(r)
        if not r["ok"]:
            print("  ", r.get("note", "?"))
        else:
            print(f"   load {r['load_s']:.1f}s | writes {r['tok_s']:.1f} tokens/s | on GPU: {gpu(r)}"
                  f" | tools: {'native' if r['native'] else 'JSON text'}")
            for want, got, tgt, dt, good, why in r["details"]:
                print(f"   want {want:<8} got {str(got):<8} {str(tgt or ''):<10} {dt:5.1f}s  {'ok' if good else 'WRONG'}")
                if why:
                    print("        ", why)
        unload(ol, m)

    print("\n" + "=" * 86)
    print(f"{'model':<16}{'GPU%':>6}{'tok/s':>8}{'load s':>8}{'decision s (median/worst)':>28}{'correct':>10}  tools")
    for r in results:
        if not r["ok"]:
            print(f"{r['model']:<16}  -- {r.get('note', '')}")
            continue
        print(f"{r['model']:<16}{gpu(r, '').strip('%'):>6}{r['tok_s']:>8.1f}{r['load_s']:>8.1f}"
              f"{r['latency_s']:>17.1f} / {r['worst_s']:<8.1f}{r['right']:>6}/{r['total']:<3}  {'native' if r['native'] else 'JSON'}")
    print("=" * 86)
    print("Copy everything from the '=== ' lines down and paste it back to me.")


if __name__ == "__main__":
    main()
