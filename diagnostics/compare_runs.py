"""compare_runs.py - one compact table for the newest runs in outputs\\logs\\ (looks in all sub-folders; reads run_meta.json,
run_summary.json, episodes.csv).

    python diagnostics\\compare_runs.py                              # newest 4 runs
    python diagnostics\\compare_runs.py 6                            # newest 6 runs
    python diagnostics\\compare_runs.py 6 outputs\\logs\\duck2          # newest 6 runs of one test only
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))   # put the project folder on the path, so that "sim", "agent_harness", "duck1", "duck2" can be imported

import csv
import json
import os
import sys
from collections import Counter

from project_paths import LOG_DIR


def load(folder):
    def js(name):
        p = os.path.join(folder, name)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    with open(os.path.join(folder, "episodes.csv"), encoding="utf-8-sig", newline="") as f:
        eps = list(csv.DictReader(f))
    return js("run_meta.json"), js("run_summary.json"), eps


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    root = sys.argv[2] if len(sys.argv) > 2 else LOG_DIR
    folders = [dp for dp, _, files in os.walk(root) if os.path.basename(dp).startswith("run_") and "episodes.csv" in files]
    folders = sorted(folders, key=os.path.getmtime)[-n:]
    head = ["run", "model", "seeds", "pnl", "grd", "mem", "kick", "ok", "steps", "miss", "gact", "llm_s", "sim_s", "ended"]
    rows = []
    for fo in folders:
        try:
            meta, summ, eps = load(fo)
        except (OSError, ValueError):
            continue
        name = os.path.basename(fo).split("_")
        ended = " ".join(f"{k}x{v}" for k, v in Counter(e["ended"] for e in eps).items())
        rows.append([name[2] if len(name) > 2 else "?", str(meta.get("model", "?")).replace("gemma4:", "g4:")[:12],
                     len(meta.get("seeds", [])), meta.get("panel", "-"), meta.get("guard", "-"),
                     eps[0].get("memory", "-") if eps else "-", meta.get("kick_after", 0),
                     f"{summ.get('successes', '?')}/{summ.get('episodes', '?')}", summ.get("mean_steps", ""),
                     summ.get("missed_goals", ""), summ.get("guard_actions", ""), summ.get("mean_llm_s", ""),
                     summ.get("mean_sim_s", ""), ended])
    widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(head)]
    line = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths))
    print(line(head))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


if __name__ == "__main__":
    main()