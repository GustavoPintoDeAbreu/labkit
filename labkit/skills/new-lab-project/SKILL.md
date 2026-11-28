---
name: new-lab-project
description: Start a new project in Gustavo's homelab so every agent and the lab catalog know about it. Use when creating a new repo, bot, service or tool for the lab.
---

# Start a new lab project

1. **Look before building.** Call `lab_overview`, then `find` for each capability you need
   (WhatsApp, LLM, Pi deploy, scheduling, backups). Reuse `labkit` and existing services.
2. **Scaffold:** `labkit init` in the repo. It writes `AGENTS.md` (the guide every agent
   reads, with the Lab block), `CLAUDE.md` (`@AGENTS.md`) and a `lab.yaml` skeleton, and
   never overwrites existing files.
3. **Fill in `lab.yaml`:** summary, `runs`, `provides` (anything another project could
   reuse), `uses` (ids from the `lab` MCP). **No IPs, ports, hostnames, chat ids or
   secrets**: it may end up in a public repo, and the catalog rejects them.
   If people will trigger it from WhatsApp (an on-demand action, a list, a switch), give it
   a command and list it under `commands:` (`{cmd: /word, where: [dm], who: owner, summary}`;
   `where: [group, dm]`, `who: everyone` for the friends). Kaya's `/help` is built from these,
   so an unlisted command is invisible. Add a test that your parser and `commands:` agree.
4. **Register it:** add the repo's path to `catalog.repos` in labwatch's `config.yaml` (PR).
5. **If it runs unattended:** deploy with the `deploy-to-pi` skill (or the PC's broker for
   GPU work), and add a labwatch collector or health rule, through a PR.
6. **If it holds data Gustavo would ask about** (prices, runs, listings, status): give its
   website a `/mcp` server (streamable HTTP, stateless JSON) with a read-only `status` tool and
   `readOnlyHint` on every read; writes are the website's own forms, annotated as writes.
   Pattern: price-scout `docs/agent/mcp.md`. Then add it to lab-assistant (its `.env`
   `ASSISTANT_MCP_SERVERS` and `assistant/agent.py` `SERVERS`), so he can ask it by voice.
7. Keep `AGENTS.md` short (commands, rules, doc index); put history and detail in
   `docs/agent/*.md` and list them under `docs:` in `lab.yaml`.
