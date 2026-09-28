"""``labkit init`` and ``labkit install-skills``: how a new project joins the lab.

``init`` writes three files into a repo, never overwriting one that exists:

- ``AGENTS.md``: the tool-neutral agent guide (Codex, Cursor, Copilot, Gemini and
  Qwen Code read it natively), starting with the Lab block that points any agent
  at the ``lab`` MCP server, or at the catalog over HTTP when MCP is missing.
- ``CLAUDE.md``: ``@AGENTS.md``, so Claude Code reads the same guide.
- ``lab.yaml``: the manifest the lab catalog reads. No addresses or secrets in
  it; refer to services by id (``find`` in the ``lab`` MCP lists them).

``install-skills`` copies labkit's agent skills into ``~/.claude/skills/``.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path
from typing import List

SKILLS = Path(__file__).resolve().parent / "skills"
MARKER = ".labkit-skill"


def hub_url() -> str:
    return (os.environ.get("LAB_HUB_URL") or "http://pi5.local:8095").rstrip("/")


def lab_block(hub: str) -> str:
    return f"""## Lab

This repo is part of Gustavo's homelab. Before building a client, script or
service, ask the `lab` MCP server what already exists (`lab_overview`, then
`find`): WhatsApp sending, the GPU broker, Pi deploys and more usually exist
already, often in `labkit`. Without MCP, read {hub}/api/catalog?format=md (LAN
only). This repo describes itself to the lab in `lab.yaml`; keep it current.
"""


def agents_md(name: str, hub: str) -> str:
    return f"""# AGENTS.md — {name}

<One paragraph: what this project is, who uses it, where it runs.>

{lab_block(hub)}
## Run things

```bash
<test command>
<run / deploy command>
```

## Rules

- <invariants an agent must never break>

## Docs index (`docs/agent/`)

| File | Read it when |
|---|---|
"""


def lab_yaml(slug: str) -> str:
    return f"""# What this repo is to the rest of the lab. Read by labwatch's catalog
# (agents query it through the `lab` MCP). Never put addresses, ports, hosts or
# secrets here: refer to services by id. Only `public` may be repeated by a chatbot.
product: {slug}
summary: "<one sentence: what it does>"
# repo: GustavoPintoDeAbreu/{slug}
runs: []           # e.g. [{{host: pi, kind: container, name: {slug}, deploy: scripts/deploy_pi.sh}}]
provides: []       # reusable pieces: [{{id: {slug}.x, kind: lib, path: ..., summary: ...}}]
uses: []           # e.g. [svc.kaya-waha, svc.llm-broker, labkit.waha]
docs:
  - {{topic: "agent guide (start here)", path: AGENTS.md}}
# public: {{built_with: "...", stats: [{{key: counter.key, label: "..."}}]}}
"""


def init(repo: Path, name: str, hub: str, out=print) -> List[Path]:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "project"
    files = {"AGENTS.md": agents_md(name, hub), "CLAUDE.md": "@AGENTS.md\n", "lab.yaml": lab_yaml(slug)}
    written = []
    for fn, text in files.items():
        p = repo / fn
        if p.exists():
            out(f"·  {fn} exists; left alone")
            if fn == "CLAUDE.md" and "@AGENTS.md" not in p.read_text():
                out("   add a line `@AGENTS.md` to it so Claude Code reads the shared guide")
            if fn == "AGENTS.md" and "## Lab" not in p.read_text():
                out("   add this to it:\n\n" + lab_block(hub))
            continue
        p.write_text(text)
        written.append(p)
        out(f"✅ wrote {fn}")
    out("Next: fill in lab.yaml and AGENTS.md; for a long-running service, add a labwatch collector or health rule (PR).")
    return written


def install_skills(dest: Path, out=print) -> List[Path]:
    """Copy each skill into ``dest``; skills labkit installed before are refreshed, others left alone."""
    dest.mkdir(parents=True, exist_ok=True)
    done = []
    for src in sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").exists()):
        target = dest / src.name
        if target.exists() and not (target / MARKER).exists():
            out(f"·  {target} exists and is not labkit's; left alone")
            continue
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        (target / MARKER).write_text("installed by labkit install-skills; rerun it to refresh\n")
        done.append(target)
        out(f"✅ skill {src.name} -> {target}")
    return done


def add_parsers(sub) -> None:
    p = sub.add_parser("init", help="add AGENTS.md, CLAUDE.md and lab.yaml to a repo (never overwrites)")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--name", help="project name (default: the folder name)")
    p.add_argument("--hub", help="labwatch hub URL (default $LAB_HUB_URL or http://pi5.local:8095)")
    p.set_defaults(func=_init_cmd)
    s = sub.add_parser("install-skills", help="copy labkit's agent skills into ~/.claude/skills")
    s.add_argument("--dest", default=str(Path.home() / ".claude" / "skills"))
    s.set_defaults(func=lambda a: install_skills(Path(a.dest)) and 0)


def _init_cmd(a: argparse.Namespace) -> int:
    repo = Path(a.path).resolve()
    init(repo, a.name or repo.name, (a.hub or hub_url()).rstrip("/"))
    return 0
