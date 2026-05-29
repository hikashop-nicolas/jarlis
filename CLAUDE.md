# JARLIS: setup wizard for AI coding agents

You are an AI coding agent (Claude Code, Codex, Gemini CLI, Cursor, Aider, or similar). The user pasted this repo's URL into your session and wants help setting up JARLIS: *Just Another Really Limited Intelligent System*, a self-hosted IMAP email-triage assistant. This file is your script; follow it.

> **Sibling file**: `CLAUDE.md` is identical to this file. Whichever your CLI prefers will be loaded first; the other is fine to ignore.

---

## Hard rule: credentials never enter your context

JARLIS is "limited by design": *the user's IMAP and SMTP passwords must never pass through any AI agent's prompt, tool call, or transcript.* When you reach the credentials step, you do **not** ask for them yourself. Instead, you instruct the user to run a standalone Python script in **their own terminal**: the script collects everything via `getpass()` (hidden input) and stores secrets in their OS keychain.

If at any point you find yourself about to elicit a password, **stop**. Tell the user to run `python -m jarlis.setup` instead, and wait.

---

## Step 0: detect state

1. List the repo root. If `config.toml` exists, JARLIS is already configured. Skip to *Routine assistance* at the bottom.
2. Otherwise: this is a first-time setup. Greet the user briefly (one sentence) and proceed.

---

## Step 1: pre-flight

Run these checks. If any fails, surface the failure and stop until the user resolves it.

```bash
python3 --version            # must be ≥ 3.11
pip install -e ".[dev]"      # install JARLIS itself + dev deps
```

If `pip install` complains about user permissions, suggest `pip install -e . --user` and continue.

You don't need to verify the AI CLI on PATH: the user is *already* talking to it (you).

---

## Step 2: hand off to `jarlis.setup` for credentials

Tell the user, in their language if you can detect it from the repo or from how they addressed you:

> "JARLIS keeps your IMAP password out of any AI agent: including me. Open a terminal, `cd` to this repo, and run:
>
> ```
> python -m jarlis.setup --lang <code>
> ```
>
> Replace `<code>` with the user's language (`fr`, `ja`, `en`, etc.) so the wizard prompts are in their language. If you can't tell what language they speak, drop the `--lang` flag and the script will auto-detect from their `$LANG` env var.
>
> It will prompt you locally (hidden input) for your name, IMAP server, password, SMTP, notification email, AI backend, and recap frequency. The password goes straight to your OS keychain. When the script finishes, come back and tell me 'setup is done'."

Wait for the user. Do not loop / keep asking. They may take a few minutes.

When they signal completion, verify:
- `config.toml` exists
- `python -m jarlis.config show` runs cleanly and prints sane values (passwords print as `***` or `unset`)

If either check fails, ask the user to re-run `python -m jarlis.setup` and try again.

If the user is unsure about app-password / IMAP setup for their email provider, point them at `PROVIDERS.md`: it has step-by-step instructions for Gmail, Outlook, Fastmail, iCloud, Yahoo, and ProtonMail. Don't try to walk them through the provider's UI yourself; the doc is more reliable than your memory of provider menus.

---

## Step 3: bootstrap memory

Tell the user what's about to happen: plainly, in their language:

> "Now I'll connect to your IMAP via the credentials you set up, scan the last N days, and use the AI to draft a memory tree of your contacts, recurring topics, and writing style. This takes a few minutes and uses some AI quota. Nothing gets sent or modified in your mailbox. After it finishes, we'll review the output together."

Read the `backlog_days` value from `config.toml` (default 30). Run:

```bash
python -m jarlis.bootstrap --days <N>
```

If bootstrap errors:

| Error pattern | Likely cause | Fix |
|---|---|---|
| IMAP auth failed | wrong password in keyring | run `python -m jarlis.config delete-password <user>` then re-run `python -m jarlis.setup` |
| `<backend>` CLI not on PATH | AI backend mismatch | edit `[ai].backend` in `config.toml` and retry |
| `no sent folder found` | provider quirk | ignore: voice exemplars are skipped, the rest works |

When bootstrap succeeds, it prints a report listing what was written.

---

## Step 4: review the memory tree with the user

Open every generated file under `memory/` in turn. **Tell the user what you found before showing the contents.** Be concrete:

> "Bootstrap found 14 contacts, 6 recurring topics, an organization summary, and a profile of you. Want to spot-check a few? You can edit any of these files at any time: they're plain markdown."

Use your `Read` tool to display each file. Suggest edits where the AI's output is generic, wrong, or invented. Edit only with explicit user permission. Pay extra attention to:

- `memory/me.md`: does it accurately describe the user's role and responsibilities?
- `memory/00_organization.md`: got the org name + conventions right?
- `memory/ignored_topics.md`: anything to silence permanently? (mailing lists, library cleanups, all-staff lunches, …)
- `memory/voice/<lang>/exemplar_*.md`: these get injected into every future draft prompt; a bad exemplar warps every reply
- `memory/auto_shared_addresses.txt`: addresses JARLIS *thinks* are shared mailboxes where multiple people post. For each entry, ask: *"JARLIS detected `<address>` as a shared mailbox where 2+ different signers were seen. Is this actually a shared address (e.g. `info@`, `bureau@`, `team@`), or one person signing under multiple name forms? Keep or remove?"* Edit the file to remove false positives. Then ask: "any shared mailboxes you know about that aren't here?" and add them.

---

## Step 5: install the scheduler

Preview before applying:

```bash
python -m jarlis.scheduler print
```

Show the output to the user. Get explicit confirmation. Then:

```bash
python -m jarlis.scheduler install
```

Three jobs are installed: pipeline (every fetch_interval), recap (daily, self-gates per `[recap].frequency`), cleanup (weekly).

Tell them how to undo: `python -m jarlis.scheduler uninstall`.

---

## Step 6: smoke run

```bash
python -m jarlis.pipeline --process-only
```

Show the user the summary report. Drafts will land in `waiting_for_approval/`; flagged emails get a line in `pending_attention.md`.

Done. Say so concisely. Remind them:
- Drafts wait for their review before anything goes out
- Memory files under `memory/` are theirs to edit
- The scheduler can be uninstalled with one command

---

## Routine assistance (when `config.toml` already exists)

The user is back. Common requests:

| Request | What you do |
|---|---|
| "Show me what JARLIS did today" | Read `pipeline.log` (last ~50 lines), summarize per-bucket counts |
| "Why did it draft this?" | Read `processed/<folder>/meta.json`, present `classification.reason` and the `why_log` |
| "This contact moved on" | `python -c "from jarlis import memory, config; memory.archive_person(config.load_config(), '<email>')"` |
| "Silence this topic" | Append a bullet to `memory/ignored_topics.md`. Next cleanup re-archives matching past emails |
| "Re-run bootstrap" | Confirm they want to *replace* current memory, then `python -m jarlis.bootstrap --days N` |
| "Change AI backend" | Edit `[ai].backend` in `config.toml`. If `llm`, also set `model` |
| "Change recap frequency" | Edit `[recap]` block. No reinstall needed: the script self-gates |

Always read the relevant file before recommending changes; the user owns their memory tree, not you.

---

## Useful commands

```bash
# Daily life
python -m jarlis.pipeline                      # one fetch+classify cycle
python -m jarlis.recap --print                 # render today's recap to stdout (no send)

# Configuration
python -m jarlis.setup                         # full interactive (re)setup
python -m jarlis.config show                   # show config (passwords redacted)
python -m jarlis.config paths                  # show config.toml search order

# Scheduler
python -m jarlis.scheduler print               # preview the would-be install
python -m jarlis.scheduler install             # apply (per-OS)
python -m jarlis.scheduler uninstall           # remove every JARLIS scheduler entry

# Maintenance
python -m jarlis.bootstrap --days 30           # re-bootstrap from N days
python -m jarlis.cleanup                       # run the weekly cleanup pass manually
```
