# JARLIS: manual setup

If you're using an AI coding agent (Claude Code, Codex, Gemini CLI, Cursor, Aider…), prefer the guided flow: paste the repo URL into the agent and let it follow `AGENTS.md`. This file is the manual fallback.

## Requirements

- Python ≥ 3.11
- An IMAP+SMTP email account at any standards-compliant provider: Gmail, Outlook/Microsoft 365, Fastmail, iCloud, Yahoo, ProtonMail (via Bridge), your own server, anything that supports IMAP+SMTP. Providers with 2FA usually require an *app password* rather than your account password: see [`PROVIDERS.md`](PROVIDERS.md) for step-by-step instructions per provider.
- One of these CLIs on `$PATH`: `claude`, `codex`, `gemini`, or `llm`. None of them needs to be on PATH at install time: only when you actually run `jarlis.bootstrap` or `jarlis.pipeline` against the AI

## 1. Install dependencies

```bash
git clone <your fork of JARLIS>
cd jarlis
pip install -e .                       # core
pip install -e ".[attachments]"        # if you want PDF/docx parsing later
pip install -e ".[dev]"                # if you plan to hack on JARLIS
```

## 2. Run the interactive setup

```bash
python -m jarlis.setup
```

This is the **recommended path**: it walks through everything (name, IMAP, SMTP, AI backend, recap frequency) and asks for your password via `getpass()` so it never echoes to the screen and never enters any AI agent's context.

When it finishes you have:

- `config.toml` written next to your current directory
- IMAP (and optionally SMTP) password stored in your OS keychain via `keyring`
- An optional one-shot test of IMAP + SMTP

## 2b. Manual config editing (advanced)

If you'd rather edit by hand:

```bash
cp config.example.toml config.toml
$EDITOR config.toml
python -m jarlis.config set-password <imap-username>
```

The `set-password` command also reads via `getpass()`: same privacy model.

## 3. Bootstrap memory from your last N days of mail

```bash
python -m jarlis.bootstrap --days 30
```

JARLIS connects to IMAP (using the keyring password), scans your inbox + sent items, and uses your chosen AI backend to write:

- `memory/00_organization.md`: org identity, language conventions
- `memory/me.md`: your profile inferred from sent items
- `memory/preferences.md`: (you write this; the example file is a template)
- `memory/ignored_topics.md`: automated-looking senders to silence
- `memory/people/<email>.md`: one per frequent correspondent
- `memory/topics/<slug>.md`: one per recurring subject
- `memory/voice/<lang>/exemplar_NN.md`: your real sent emails as drafting style

**Review every file** before going live. Anything inaccurate gets injected into every future draft.

## 4. Send a test notification

```bash
python -c "from jarlis import notify, config; print(notify.send_test_ping(config.load_config()))"
```

Confirm an email arrived at `[notification].to`. (`jarlis.setup` offers to do this for you.)

## 5. Install the scheduler

```bash
python -m jarlis.scheduler print     # preview
python -m jarlis.scheduler install   # apply
```

This installs three jobs on your OS scheduler:

| Job | Cadence | What it does |
|---|---|---|
| pipeline | every `[pipeline].fetch_interval` (default 20m) | fetch IMAP, classify, route, draft |
| recap    | daily at `[recap].time`: script self-gates per `[recap].frequency` | sends recap email |
| cleanup  | weekly on `[cleanup].run_on_weekday` | ages out stale memory, deletes old bodies |

To remove later: `python -m jarlis.scheduler uninstall`.

Cross-platform: launchd on macOS, cron on Linux, Task Scheduler on Windows.

## 6. (Optional) Run one cycle manually

```bash
python -m jarlis.pipeline
```

Watch the log. Drafts appear in `waiting_for_approval/`. Flagged emails get a line in `pending_attention.md`.

## What lives where

```
config.toml                    your settings (gitignored)
memory/                        editable markdown that JARLIS reads at every AI call
  ├── 00_organization.md
  ├── me.md
  ├── preferences.md
  ├── ignored_topics.md
  ├── people/<email>.md
  ├── topics/<slug>.md
  ├── voice/<lang>/exemplar_NN.md
  └── archive/                 aged-out files (recoverable)

email/                         transient + permanent email storage
  ├── inbox/<folder>/          fetched but not yet processed
  ├── processed/<folder>/      classified; the source of retrieval context
  ├── processed/archived/      archive bucket
  ├── processed/spam/          silent
  ├── attachments/<folder>/    extracted attachments (stable paths)
  └── error/                   processing failures

waiting_for_approval/<slug>.md the queue of AI drafts you should review
pending_attention.md           one-line entries for flagged-without-draft items
snoozed/                       (you create this) emails to revisit later

*.log                          rotating logs (pipeline.log, etc.)
```

## Common operations

```bash
# Re-run setup from scratch
python -m jarlis.setup

# Re-bootstrap (overwrites current memory)
python -m jarlis.bootstrap --days 60

# Manually run the weekly cleanup
python -m jarlis.cleanup

# Print today's recap without sending
python -m jarlis.recap --print

# See what the scheduler installed
python -m jarlis.scheduler print
```

## Uninstall

```bash
python -m jarlis.uninstall
```

Removes scheduler entries (cron / launchd / Task Scheduler), optionally clears the keyring entries, and prints exactly which directories under your project root contain user data so you can delete them when ready. The script does **not** delete your memory tree, drafts, or email archive automatically: it only shows you the `rm` commands.

Flags:
- `--scheduler-only`: only remove scheduler entries, nothing else
- `--yes`: don't prompt; remove scheduler + keyring entries automatically

## Troubleshooting

- **IMAP auth failed** → verify username; on providers with 2FA (Gmail, Fastmail, iCloud, Outlook…) you usually need an *app password*, not your normal account password
- **AI backend not on PATH** → install the chosen CLI or change `[ai].backend`
- **No emails in `processed/`** → check `pipeline.log` for fetch errors; check `seen_email_ids.json` for dedup state
- **Stuck pipeline alert** → emails accumulating in `email/inbox/` across runs; usually means the AI backend is timing out: check `pipeline.log`
- **Recap email never arrives** → check SMTP creds; run the test ping
- **The AI keeps drafting in the wrong tone** → review `memory/voice/<lang>/`; add or replace exemplars from your actual sent emails

## Privacy

JARLIS only ever talks to:
- Your IMAP server (read-only by default)
- Your SMTP server (only when sending notifications you configured)
- Your chosen AI CLI (whatever subprocess that CLI itself talks to)

Nothing else. No telemetry, no analytics, no remote logging. Your IMAP/SMTP passwords are collected by the local `jarlis.setup` script and stored in your OS keychain: they never appear in any AI agent's prompt or transcript.
