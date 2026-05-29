# JARLIS

*Just Another Really Limited Intelligent System*: a self-hosted, IMAP-based AI email assistant.

The "limited" is a design principle, not a disclaimer. JARLIS:

- Uses your AI CLI **read-only** by default
- Lands AI-drafted replies in `waiting_for_approval/` for **your** review: never auto-sends
- Keeps memory as plain markdown files **you can edit**
- **Never lets your IMAP/SMTP password enter an AI agent's context**: credentials are collected by a standalone Python script, not the AI
- Stores credentials in your **OS keychain**, not in plaintext
- Has **no telemetry**. Talks only to your IMAP/SMTP server and the AI CLI you pick

## Email providers

JARLIS works with **any IMAP+SMTP provider**: Gmail, Outlook/Microsoft 365, Fastmail, iCloud, Yahoo, ProtonMail (via Bridge), self-hosted IMAP servers, anything standards-compliant. The interactive setup auto-detects common providers from your email address; for everything else you enter the server settings manually.

Most providers with 2FA enabled require an **app password** rather than your account password. See [`PROVIDERS.md`](PROVIDERS.md) for step-by-step instructions covering each supported provider.

## Quick start (with an AI coding agent)

JARLIS ships with an `AGENTS.md` (also mirrored as `CLAUDE.md`) that any AI coding CLI can read as a setup wizard. Pick whichever you have:

| CLI | Reads | Launch |
|---|---|---|
| Claude Code | `CLAUDE.md` | `claude` |
| OpenAI Codex | `AGENTS.md` | `codex` |
| Gemini CLI | `AGENTS.md` (or `GEMINI.md`) | `gemini` |
| Cursor | `AGENTS.md` (or `.cursorrules`) | open the repo in Cursor |
| Aider | `AGENTS.md` | `aider` |

Then point it at this repo:

```
> Set up https://github.com/<your-fork>/jarlis for me
```

The agent walks you through pre-flight, hands credential collection off to a local script (so your password **never** enters its context), runs bootstrap, reviews the auto-extracted memory with you, and installs the scheduler.

## Quick start (manual, no AI)

```bash
git clone <your fork>
cd jarlis
pip install -e .

python -m jarlis.setup           # interactive: asks for everything; password via getpass
python -m jarlis.bootstrap       # auto-extracts memory from your last N days of email
$EDITOR memory/*.md              # review & edit
python -m jarlis.scheduler install
```

Full manual instructions are in [`SETUP.md`](SETUP.md). Provider-specific app-password setup (Gmail, Outlook, Fastmail, iCloud, Yahoo, ProtonMail) is in [`PROVIDERS.md`](PROVIDERS.md).

## Uninstall

```bash
python -m jarlis.uninstall                  # interactive: removes scheduler + (optionally) keyring; tells you what folders to delete
python -m jarlis.uninstall --scheduler-only # just remove the cron / launchd / Task Scheduler entries
python -m jarlis.uninstall --yes            # don't prompt; clear scheduler + keyring
```

The script never deletes your memory tree or email archive automatically. It prints exact `rm` commands you can run yourself when you're ready.

## Architecture

```
IMAP fetch → cascading classifier → 3-bucket triage → draft via AI CLI → notify → recap on schedule
```

| Concern | Implementation |
|---|---|
| AI backends | `claude`, `codex`, `gemini`, `llm`: one Protocol, one file each in `src/jarlis/ai/` |
| Triage buckets | `drafted` (reply prepared) / `flagged` (note for follow-up) / `archive` (silenced or low-priority) |
| Classifier | hash-cache → declarative rules from `memory/ignored_topics.md` → LLM for ambiguous cases. Each layer logs its reason. |
| Memory | plain-markdown tree under `memory/`: organization, you, contacts, topics, voice exemplars |
| Retrieval | structured (same-sender + thread walk + topic-tag): no embeddings in v1 |
| i18n | TOML files per language under `src/jarlis/i18n/` (en, fr, ja shipped) |
| Recap | daily / every_n_days / weekly / monthly / custom-cron: script self-gates |
| Cleanup | weekly: ignored-topic re-classification + memory aging + old-body retention |
| Scheduler | cross-platform: launchd / cron / Task Scheduler |

## Status

Alpha. The full design is in [`PLAN.md`](PLAN.md). 130+ unit tests; end-to-end testing against a real Gmail account is each user's call.

## Configuration

A single `config.toml` at the project root, your XDG dir, or `~/.jarlis/`. The interactive `python -m jarlis.setup` writes one for you. See `config.example.toml` for the annotated schema.

## Languages

JARLIS handles two language axes independently:

- **User-facing UI language** (recap subjects/bodies, stuck-pipeline alerts, setup-wizard prose). Picked from `[user].languages[0]` in `config.toml`. Strings live in `src/jarlis/i18n/<lang>.toml`. Three are shipped (`en`, `fr`, `ja`); adding another is one new TOML file copied from `en.toml`. Missing keys fall back to English.
- **Draft language** (the language JARLIS replies in). Detected per-incoming-email by the bundled `langdetect` library (~55 languages out of the box, no extra install). A built-in keyword heuristic covers en/fr/es/de/it/pt/ja/zh/ko/ru/ar/he as a fallback if the import fails. Voice exemplars matching the detected language are injected into the prompt; if none exist for that language the model still replies in the right language but without your specific style.

Memory files (`memory/me.md`, `memory/people/...`, etc.) are *user-written* in whatever language fits.

## Security

JARLIS handles untrusted email content and uses an AI subprocess. Read [`SECURITY.md`](SECURITY.md) before deploying against a mailbox you care about. Highlights:

- Prompts wrap email content in untrusted-input delimiters and warn the model against following instructions inside
- AI subprocesses run with read-only sandboxes where supported (`claude --allowedTools Read`, `codex --sandbox read-only`)
- Drafts wait in `waiting_for_approval/` for your review; nothing is auto-sent
- Credentials live in your OS keychain, never in any AI's context
- No `shell=True`, no telemetry, no HTML email, no remote fetches

## License

MIT: see [`LICENSE`](LICENSE).
