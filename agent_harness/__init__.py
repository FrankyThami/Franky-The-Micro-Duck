"""agent_harness - everything that runs an LLM agent on a duck, for both test sets.

    franky_agent.py   the brains (OllamaBrain = the LLM, RuleBrain = no LLM), the first tool set and prompt
    harness.py        the agent loop: runs one episode, uses the tracker and the guardrails
    state_tracker.py  SEES  - after every tool call checks the TRUE state (did it work? anything unexpected? looping?)
    guardrails.py     ACTS  - table-edge refusal, false-"done" rejection, loop/failure/time budgets, LLM retry
    run_harness.py    the run logger (events.jsonl, steps.csv, episodes.csv ...) and small helpers
    memory.py         Franky's notebook of lessons (LessonStore, MemoryBrain)
    experiment.py     the memory experiment (train / test), and the logger the scene benchmarks reuse
"""
