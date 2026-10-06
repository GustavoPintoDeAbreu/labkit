# labkit

Small shared pieces for the projects in Gustavo's homelab (a GPU PC and a Raspberry Pi 5),
so each project stops carrying its own copy.

| Piece | What |
|---|---|
| `labkit.waha` | WhatsApp through a self-hosted [WAHA](https://waha.devlike.pro): text and voice notes, retry on 5xx, fail fast on 4xx, and a `MockNotifier` twin for development |
| `labkit.broker` | The local LLM broker (llama-swap): `LLM.for_model(name).chat(...)`, `running()`, `unload()` |
| `labkit.reddit` | Reddit through its public RSS feeds: `fetch_post(url, user_agent=...)` (permalinks, `redd.it`, app share links) with top comments and outbound links; one request per 65 s, stops on 403/429 |
| `labkit.web` | `fetch_page(url)`: readable text of a page (trafilatura, needs `labkit[web]`), size-capped, never raises; GitHub repo links give the README. Public addresses only: a host or redirect hop resolving to a private, loopback, link-local or Tailscale address gives `None` (`allow_private=True` to opt out) |
| `labkit deploy-pi` | Ship a repo's `deploy/pi` compose project to the Pi: clean-tree guard, version stamp, rsync, build and restart |
| `labkit init` | Join a repo to the lab: `AGENTS.md` (with the Lab block), `CLAUDE.md` → `@AGENTS.md`, and a `lab.yaml` skeleton |
| `labkit install-skills` | Agent skills (`send-whatsapp`, `use-llm-broker`, `deploy-to-pi`, `new-lab-project`) into `~/.claude/skills` |

## Install

Pinned to a release tarball, so slim Docker images need no `git`:

```
labkit @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.3.0.tar.gz
labkit[web] @ https://github.com/GustavoPintoDeAbreu/labkit/archive/refs/tags/v0.3.0.tar.gz
```

(add `[web]` to use `labkit.web`)

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q
```

A release is a tag (`vX.Y.Z`, matching `pyproject.toml` and `labkit/__init__.py`). Projects
bump their pin on purpose; nothing follows `main`.

Addresses, chat ids and keys never live here: they come from each project's `.env`,
arguments, or environment variables (`LAB_BROKER_URL`, `LAB_HUB_URL`, `PI_HOST`).
