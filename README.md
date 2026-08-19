# Fenshen · Local-first AI Team Manager

> Your digital twin + an AI team that runs on **your own computer**. Every project is a group chat; the Meta-Agent (元神) dispatches specialist roles, tracks progress, and delivers verifiable results — files, tests, coverage reports, all persisted locally in SQLite.

**中文版** · [中文 README](./README.zh-CN.md) · [Website](http://39.105.6.135/) · [Demo Video](./site/demo-v6.4.mp4)

---

## Why Fenshen

Most AI assistants answer in text. Fenshen **executes**: it writes files, runs commands, opens a browser, tests its own work, and only claims "done" when a verifiable deliverable exists — backed by real evidence (pytest logs, coverage reports, git commits).

- 🗣️ **Group chat = team**. Say "build a login page" in a project chat; the Meta-Agent splits it into cards, dispatches frontend/backend/tester roles, and the board updates live.
- 🧠 **Your digital twin (元神)**. Trained from your resume, chats, docs and feedback (distillation with consent), bound by a 5-article constitution that always protects *you*.
- 🔄 **Self-evolving**. Every task distills reusable experience scored on 5 dimensions; promotion / demotion / guardrails keep the memory safe from day one.
- 🎚️ **Token-tiered (v0.64.14)**. Simple Q&A is answered directly by the Meta-Agent in a single call (0.7s, ~500 tokens); simple tasks go to a single role; only complex engineering tasks spin up the full team with quality gates. Same task measured: **198.9k → 9.2k tokens (21.6× less), 201s → 4.6s (43× faster)**.
- 🔒 **Local-first & safe**. Binds to 127.0.0.1 by default, token auth, dangerous commands require real human approval, and a runtime constitutional guard that cannot be downgraded by any config.

## Features

| Area | Highlights |
|---|---|
| Meta-Agent (元神) | Personality grounding, distillation (6 material types / consent model / quality score / digital clones), 5-article constitution, final acceptance gate A1-A5 |
| Group chat + Kanban | Module × stage matrix, role views, blast-radius routing, board↔chat two-way sync, topic isolation |
| Self-evolution | 5-dim experience scoring, promotion/demotion/guardrails, memory archive & panel, skill library (13 built-ins) |
| Multi-model | Per-role model config (DeepSeek / OpenAI / Claude / Ollama) with fallback chain; shared-context prefix caching for same-model teams |
| Token tiers | T0 instant answer · T1 single-role fast path · T2 full team with quality gates · T3 long-running autonomy |
| Toolchain | exec_command / browser actions / file read·write·search, all reachable from chat |
| Security | 127.0.0.1 default, local token auth, human approval on dangerous commands, cleanup backups, full audit log |

## Quickstart

**macOS**: download [分身-v6.4-macOS.dmg](site/分身-v6.4-macOS.dmg), drag to Applications, run. Browser opens `http://localhost:8002/` automatically.

**Windows**: download the exe zip, unzip and run `Fenshen.exe`.

**From source**:
```bash
git clone https://github.com/fenshenplus/fenshen-os.git
cd fenshen-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./启动分身.command   # or: uvicorn backend.main:app --port 8002
```

## Architecture

```
Desktop engine (this repo)          Mobile / other device (optional)
┌────────────────────────────┐      ┌────────────────────────────┐
│ FastAPI + SQLite (local)   │      │ H5 relay over WebSocket     │
│ ┌──────────────────────┐   │      │ chat · kanban · voice       │
│ │ Meta-Agent (元神)      │◄──┼──────┤ (communication only,      │
│ │ dispatch / judge      │   │      │  no model, no permissions)│
│ ├──────────────────────┤   │      └────────────────────────────┘
│ │ roles: architect /    │   │
│ │ backend / frontend /  │   │
│ │ tester (real LLM)     │   │
│ ├──────────────────────┤   │
│ │ tools: exec / browser │   │
│ │ / file · quality gate │   │
│ └──────────────────────┘   │
└────────────────────────────┘
```

- **Data never leaves your machine** (SQLite, no cloud).
- Default port **8002** (avoids 8000 production clashes).

## Security model

- Binds to `127.0.0.1` by default; LAN requires explicit `FENSHEN_ALLOW_LAN=1` + token.
- First launch generates a local token (0600 perms); all APIs require it.
- Dangerous commands (`rm -rf`, `git push --force`, disk formatting, …) require a **system-level human confirmation**; client-side confirmation is ignored; timeout = deny.
- **Constitution guard (v6.4)**: 5 non-overridable articles — sole principal is the local user; conflicts favor the user; conservative on money/privacy/reputation/irreversible actions; user retains full control; distilling others requires explicit consent.
- Destructive cleanup auto-backs-up the database first; full audit log.

## Token-tier design (v0.64.14)

| Tier | Trigger | Cost (measured) |
|---|---|---|
| T0 · Instant answer | Q&A / small talk | 1 call · ~500 tok · 0.7s |
| T1 · Single-role fast path | Small, explicit tasks | ~4 calls · ~9.2k tok · 4.6s |
| T2 · Full team | Complex / cross-role engineering | quality gates + acceptance (higher cost, verifiable delivery) |
| T3 · Long-running | Project-level autonomy | amortized across tasks, parallel (≤3), unattended |

Per-project override available (`product_meta.tier`), plus a tier selector in the UI. Same-model teams reuse a shared context prefix (DeepSeek prefix caching) to cut multi-role overhead.

## Roadmap

- [x] v4.0 security hardening · v5.x code capability D1-D10 · v6.0 new UI
- [x] v6.1 matrix kanban + autonomy scheduler · v6.2 cockpit P0-P3
- [x] v6.3 kanban iteration · v6.4 distillation v2 + constitution + self-evolution
- [x] v6.4.14 token tiers (instant answer / fast path / team)
- [ ] Email sign-up (international) · English UI · Windows v6.4 build · webhooks & external integrations

## License

MIT © 2026 Fenshen Project. See [LICENSE](LICENSE).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for responsible disclosure.
