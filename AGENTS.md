# AGENTS.md — labkit

Shared Python package for the lab's projects (see README.md for what it holds). It is
**public**: never put IPs, chat ids, keys, or anything about the group's members here.
Defaults that are addresses come from env vars (`LAB_BROKER_URL`, `LAB_HUB_URL`, `PI_HOST`).

## Lab

This repo is part of Gustavo's homelab. Before adding a module, ask the `lab` MCP server
what already exists (`lab_overview`, then `find`). This repo describes itself to the lab
in `lab.yaml`; add every new module to its `provides`.

## Rules

- Lift code from a project that already runs it in production, keep its behavior (and
  its tests' expectations), and migrate that project to labkit in the same round.
- Stay small: stdlib + `httpx`. Each module is usable alone.
- A release is a tag `vX.Y.Z` (bump `pyproject.toml` and `__init__.__version__` together);
  projects pin the tarball URL, so a change reaches nobody until they bump the pin.
- `.venv/bin/pytest -q` must pass; CI runs 3.11-3.13.

## Docs index

| File | Read it when |
|---|---|
| `labkit/*.py` module docstrings | using or changing a module |
| `labkit/skills/*/SKILL.md` | changing what agents are told to do |
