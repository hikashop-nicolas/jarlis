# JARLIS: Plan & Design Decisions

**JARLIS** = *Just Another Really Limited Intelligent System*

A self-hosted, IMAP-based, AI-assisted email triage tool. The "limited" is a design principle, not a disclaimer: the system stays narrow, transparent, and never takes destructive action without human approval.

JARLIS started as the generalization of an internal email-pipeline tool that worked for exactly one person on exactly one OS. It is now the generic, publishable, multi-user, multi-org, multi-OS version.

---

## 1. Guiding principles ("limited" by design)

- **Read-only AI tooling by default.** AI backends invoked with restricted permissions (Claude `--allowedTools Read`, Codex `--sandbox read-only`).
- **No auto-send.** All AI-drafted replies land in `waiting_for_approval/` for human review.
- **Memory is human-readable markdown.** No opaque DBs. The user can edit anything.
- **No tasks.** Drafts are the queue. No parallel to-do system to maintain.
- **Credentials in OS keychain.** Plaintext config.toml fallback only when keyring unavailable.
- **No telemetry.** No remote calls beyond the user's IMAP / SMTP / chosen AI CLI.
- **Bounded storage.** Periodic cleanup so the system doesn't accumulate forever.
- **Explainable.** Every classification logs a one-line reason the user can read.

---

## 2. Architecture (one-liner)

```
IMAP fetch  →  cascading classifier  →  three-bucket triage  →  draft (if needed) via AI CLI  →  notify  →  recap on schedule
```

Key abstractions:
- **AI backend interface**: claude / codex / gemini / llm, all behind one `call(prompt) → text` Protocol.
- **Memory tree**: versioned markdown files the user owns; AI reads them, AI proposes additions, user approves.
- **Triage state** lives in IMAP folders + small per-email metadata JSON, not a parallel DB.

---

## 3. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Project name: **JARLIS** | User chose; "limited intelligence" is a design constraint |
| 2 | License: **MIT** | Permissive, GitHub-friendly, no patent fuss |
| 3 | Language: **Python 3.11+** | `tomllib` in stdlib; broadest CLI ecosystem; cross-platform |
| 4 | Config format: **TOML** | Human-editable, native parsing, comments allowed |
| 5 | i18n format: **TOML files per language** in `src/jarlis/i18n/{en,fr,ja}.toml` | Same parser as config |
| 6 | Credentials: **`keyring` library, fallback to config.toml** | OS keychain on all 3 OSes; degrade gracefully |
| 7 | AI backends: **claude, codex, gemini, llm** behind one Protocol | All four have non-interactive modes (verified) |
| 8 | Memory split: **`00_organization.md`, `me.md`, `people/<email>.md`, `topics/<slug>.md`, `preferences.md`, `ignored_topics.md`** | Lets bootstrap auto-extend without touching user-curated files |
| 9 | **No standalone task system.** Drafts in `waiting_for_approval/` are the queue. Snoozes go to `snoozed/`. | Tasks pile up because they have no completion signal; drafts do |
| 10 | Three-bucket triage: **`archive` / `drafted` / `flagged`** | Shortwave Method; clearer mental model |
| 11 | **Cascading classification**: hash-cache → rules → embeddings → LLM | Cheaper layers handle deterministic cases; LLM only for ambiguous |
| 12 | **Per-email "why" log** in metadata JSON | Trust + debuggability |
| 13 | **Voice exemplars**: store ~5–10 representative sent emails per language; inject in draft prompts | Better than a "writing style" description; model copies actual phrasing |
| 14 | **RAG**: structured retrieval only in v1 (same-sender history + thread walking + topic-tag matching). Embeddings deferred to v1.5 if needed. | Avoids unnecessary complexity; embeddings via `llm embed -m nomic-embed-text` if added |
| 15 | **Recap frequency configurable**: daily / every_n_days / weekly / monthly / custom-cron. Scheduler runs daily, script self-gates. | One scheduler entry; frequency change via config edit, no reinstall |
| 16 | **Storage cleanup**: ignored-topic list (proactive) + time-based aging (90d people / 60d topics / 14d drafts / 365d emails) | Prevents accumulation; recap of what was archived included weekly |
| 17 | Cross-platform scheduler: detects OS, installs Task Scheduler / launchd / cron | macOS / Linux / Windows from one installer |

---

## 4. Memory tree

```
memory/
├── 00_organization.md        ← org identity, public URL, language conventions, recurring topics
├── me.md                     ← user's profile: name variants, role, languages, writing prefs
├── preferences.md            ← email style: tone, signature, formatting rules
├── ignored_topics.md         ← user-maintained list of topics to silence
├── people/
│   ├── <email>.md            ← one file per contact; auto-extended by bootstrap, user-edited
│   └── ...
├── topics/
│   ├── <slug>.md             ← recurring threads (subject patterns + sender fingerprints)
│   └── ...
├── voice/
│   ├── <lang>/               ← representative sent emails as drafting exemplars
│   │   ├── exemplar_01.md
│   │   └── ...
└── archive/                  ← cleaned-up files moved here, not deleted
    ├── people/
    └── topics/
```

Memory loader (`src/jarlis/memory.py`) concatenates 00, me, preferences, plus relevant people + topics + voice exemplars into the AI prompt at draft time.

---

## 5. Repo layout

```
jarlis/
├── README.md                       ← public-facing landing; "paste this URL into Claude Code"
├── CLAUDE.md                       ← setup wizard prompt FOR Claude (the magic file)
├── SETUP.md                        ← human-readable manual fallback
├── PLAN.md                         ← this file (kept in repo as design ref)
├── LICENSE                         ← MIT
├── .gitignore                      ← excludes config.toml, memory/, email/, *.log, secrets
├── config.example.toml             ← annotated template
├── pyproject.toml                  ← deps with optional groups (matplotlib/pypdf/python-docx for utilities)
├── src/jarlis/
│   ├── __init__.py
│   ├── config.py                   ← TOML loader, keyring integration, fallback logic
│   ├── memory.py                   ← read/write memory tree
│   ├── i18n/
│   │   ├── __init__.py             ← t(key, **kwargs) loader with en fallback
│   │   ├── en.toml
│   │   ├── fr.toml
│   │   └── ja.toml
│   ├── ai/
│   │   ├── base.py                 ← Protocol: call(prompt, output_format) -> str
│   │   ├── claude.py
│   │   ├── codex.py
│   │   ├── gemini.py
│   │   └── llm_cli.py
│   ├── classify.py                 ← cascading classifier (rules → embeddings* → LLM)
│   ├── retrieve.py                 ← structured retrieval: same-sender + thread + topic-tag
│   ├── imap_fetch.py               ← was fetch_emails.py
│   ├── pipeline.py                 ← orchestrator: fetch → classify → triage → draft
│   ├── process_emails.py           ← per-email logic: classify, route, draft if drafted
│   ├── recap.py                    ← was daily_recap.py; configurable frequency
│   ├── notify.py                   ← SMTP via i18n strings
│   ├── bootstrap.py                ← seed memory from N days of email history
│   ├── cleanup.py                  ← scheduled aging + archive
│   └── safe_names.py               ← FAT32-safe filename generator
├── scripts/
│   ├── install_scheduler.py        ← detects OS, installs job
│   └── templates/
│       ├── jarlis.plist            ← macOS launchd template
│       ├── jarlis.cron             ← cron template
│       └── jarlis_task.xml         ← Windows Task Scheduler template
├── tests/
│   ├── test_classify.py
│   ├── test_memory.py
│   ├── test_pipeline.py
│   └── fixtures/                   ← anonymized sample emails
└── examples/
    └── memory/                     ← anonymized sample memory tree

(*) Embeddings layer deferred to v1.5; classifier exposes the slot but uses
    structured retrieval only in v1.
```

---

## 6. Configuration (`config.toml`)

```toml
[user]
firstname    = "Alice"
lastname     = "Smith"
firstname_alt = ""                # optional non-Latin variant (e.g. "アリス")
lastname_alt  = ""
email        = "user@org.tld"
languages    = ["en"]             # native first; bootstrap may suggest more

[organization]
name = "ACME"
url  = "https://acme.example/"

[imap]
server   = "imap.gmail.com"
port     = 993
username = "user@gmail.com"
# Password lives in keyring; only if keyring unavailable, fallback:
# password = "..."  ← discouraged

[smtp]
server = "smtp.gmail.com"
port   = 587

[notification]
to = "user@notification.tld"

[ai]
backend = "claude"          # claude | codex | gemini | llm
model   = ""                # required for `llm` backend; ignored otherwise

[pipeline]
max_per_run    = 10
fetch_interval = "20m"      # used by scheduler installer
backlog_days   = 30         # bootstrap window (only used at first run)

[recap]
enabled       = true
frequency     = "daily"     # daily | every_n_days | weekly | monthly | custom
n_days        = 2           # only when frequency = every_n_days
weekday       = "fri"       # only when frequency = weekly
day_of_month  = 1           # only when frequency = monthly
time          = "18:00"     # local time for the daily trigger
custom_cron   = ""          # power-user override

[cleanup]
enabled                 = true
people_archive_days     = 90
topic_archive_days      = 60
draft_pending_days      = 14
processed_email_keep_days = 365
run_on_weekday          = "sun"   # weekly cleanup pass
```

---

## 7. Setup flow (the "paste GitHub URL into Claude Code" experience)

`CLAUDE.md` at repo root is **a prompt for Claude itself**, not for humans. When a user clones JARLIS and opens it in Claude Code, Claude reads CLAUDE.md and walks them through:

**Step A: minimal questions (AskUserQuestion):**
1. Name(s): first/last + non-Latin variants if any
2. IMAP server / port / username / password (→ keyring)
3. Backlog days for bootstrap (default 30)

**Step B: auto-extract bootstrap (`python -m jarlis.bootstrap --days N`):**
- Connect to IMAP, fetch last N days
- AI extracts:
  - Distinct correspondents → `memory/people/<email>.md`
  - Recurring threads → `memory/topics/<slug>.md`
  - Org identity from internal email patterns → `memory/00_organization.md`
  - User's writing style from sent emails → `memory/me.md` + `memory/voice/<lang>/exemplar_*.md`
  - Suggested ignored topics (e.g. mailing-list noise) → user reviews → `memory/ignored_topics.md`

**Step C: review with user:**
Claude opens each generated memory file, summarizes ("I found 14 contacts and 6 recurring topics"), asks the user to spot-check / edit before finalizing.

**Step D: finish setup:**
- Notification email
- AI backend choice (verify CLI is on PATH; offer to install if not)
- Scheduler installation
- Send test notification

Bootstrap deliberately does NOT ask questions before showing what it found: that's the differentiator vs every other config-heavy tool.

---

## 8. Triage model (the three buckets)

Each classified email lands in exactly one bucket, with an `archive_reason` sub-flag if `archive`:

| Bucket | Recap behavior | Storage |
|---|---|---|
| `drafted` | Full draft preview | Email → `processed/`, draft → `waiting_for_approval/<slug>.md` |
| `flagged` | Bullet line + one-line reason | Email → `processed/`, line in `pending_attention.md` |
| `archive` + `ignored_topic` | "📚 Topic X silenced (N): subject1, subject2, ..." | Email → `processed/archived/` |
| `archive` + `low_priority` | Brief bullet list | Email → `processed/archived/` |
| `archive` + `resolved` | Silent | Email → `processed/archived/` |
| `archive` + `spam` | **Silent (only fully-quiet bucket)** | Email → `processed/spam/` |

User retains awareness of ignored-topic emails (1-line summary) but they don't generate drafts or flags.

---

## 9. Cascading classification

For each new email, layers run in order; first one with high confidence wins:

1. **Hash cache**: exact-match dedup of seen messages (rare but cheap)
2. **Rules**: declarative `from:domain.com` / `subject contains` patterns from `memory/topics/*.md` and `ignored_topics.md`. Handles ~50–70% of typical traffic.
3. **Structured retrieval** (was "embeddings": see §10): finds prior similar threads; if the prior was classified, propose same bucket
4. **LLM fallback**: full prompt with memory context; only invoked when above layers can't decide

Each step records its decision + reason in the email's metadata JSON (`why`-log).

---

## 10. Context retrieval (RAG)

**v1: structured retrieval only.** When drafting a reply, gather:
- Last N emails from the same sender (chronological)
- Full thread by walking In-Reply-To / References headers
- Any `memory/topics/<slug>.md` whose subject keywords or sender fingerprints match
- Voice exemplars for the target language

Inject as `Past similar exchanges:` block in the draft prompt.

**v1.5 (deferred): semantic embeddings.** If structured retrieval misses semantically-similar-but-keyword-different threads:
- Embedder: `llm embed -m nomic-embed-text` via Ollama (local), fallback to OpenAI's `text-embedding-3-small`
- Storage: numpy in-memory index persisted to `memory/index.npz` (cosine similarity); upgrade path to sqlite-vec or chroma at >20k emails
- Same `retrieve_context()` interface; only the implementation changes.

---

## 11. Storage cleanup (`cleanup.py`, weekly)

Two layers:

**Proactive: `memory/ignored_topics.md`**:
User-maintained markdown list. Format:
```markdown
- 図書係 cleanup announcements
- All-staff lunch coordination
- Birthday wishlist threads
```
Classifier matches new emails against entries (sender + subject + body keywords). Match → `archive` + `ignored_topic`. Past emails matching new entries are retroactively re-classified on next cleanup pass.

**Reactive: time-based aging:**
- `memory/people/<email>.md` no in/out activity in **90 days** → `memory/archive/people/`
- `memory/topics/<slug>.md` no match in **60 days** → `memory/archive/topics/`
- `waiting_for_approval/<draft>.md` pending **>14 days** → flagged in next recap
- `processed/<email>/` body files older than **365 days** → optionally deleted (config flag, default keep)

Re-promotion is automatic: if archived contact emails again, file moves back to `memory/people/`.

Weekly cleanup recap shows what was archived; user can recover anything within the next 7 days from `memory/archive/`.

---

## 12. AI backend interface

`src/jarlis/ai/base.py`:
```python
from typing import Protocol, Literal

class AIBackend(Protocol):
    name: str
    def call(self, prompt: str, *, output_format: Literal["text", "json"] = "text") -> str: ...
```

| Backend | Invocation | JSON | Auth |
|---|---|---|---|
| `claude` | `claude -p --output-format text\|json --allowedTools Read` | yes | `claude` login |
| `codex`  | `codex exec "<prompt>"` (stdout = final msg, stderr = progress) | manual extract | `CODEX_API_KEY` env |
| `gemini` | `gemini -p "<prompt>" --output-format json` | yes | Google login or API key |
| `llm`    | `llm "<prompt>" -m <model>` | manual extract | varies by provider plugin |

Each backend is one file under `src/jarlis/ai/`. Adding a new backend = one file. The bootstrap setup wizard verifies the chosen CLI is on PATH and offers install instructions if not.

---

## 13. Phased implementation

| Phase | Deliverable | Estimate |
|---|---|---|
| 1 | Repo scaffold, `pyproject.toml`, `.gitignore`, `config.example.toml`, `safe_names.py` port, basic `config.py` with keyring | half day |
| 2 | i18n loader + `en.toml` (canonical) + `fr.toml` + `ja.toml` | quick |
| 3 | Memory tree module (`memory.py`) + example memory + tests | half day |
| 4 | AI backend Protocol + `claude.py` + `llm_cli.py` (codex + gemini stubs that just shell out) | half day |
| 5 | Cascading classifier (`classify.py`): rules + LLM layers; structured retrieval (`retrieve.py`); per-email metadata JSON with why-log | full day |
| 6 | IMAP fetch (`imap_fetch.py`) cleaned of org-specific paths | half day |
| 7 | Pipeline orchestrator (`pipeline.py`) wiring fetch → classify → route → draft; three-bucket triage logic | half day |
| 8 | Voice exemplar extraction in bootstrap; injection into draft prompts | half day |
| 9 | `bootstrap.py`: minimal Q&A, IMAP scan, AI extraction, user review loop | full day (biggest unknown) |
| 10 | Recap (`recap.py`): configurable frequency, self-gating logic, all bucket categories represented | half day |
| 11 | Notification (`notify.py`): SMTP with i18n, stuck-pipeline alert | quick |
| 12 | Cleanup (`cleanup.py`): ignored-topic re-classification + time-based aging | half day |
| 13 | Cross-platform scheduler installer (`scripts/install_scheduler.py`) | half day |
| 14 | `CLAUDE.md` setup wizard prompt + `README.md` + `SETUP.md` | half day |
| 15 | End-to-end dry-run against real IMAP credentials locally (state never committed) | half day |
| 16 | Push to GitHub, test the "paste URL" flow with a fresh Claude Code session | quick |

Total: ~7–8 working days of focused effort.

---

## 14. Out of scope for v1

- **MCP server mode** (Inbox-MCP-style): keep data model clean enough to bolt on later
- **Embedding-based RAG**: see §10 above
- **Bulk unsubscribe**: Inbox-Zero-style feature for personal mail; not a fit for org workflow
- **TUI dashboard**: `waiting_for_approval/` markdown + Claude Code session is the UI
- **Calendar integration**: scope creep
- **Multi-org config**: one user, one org per install in v1; multi-org would just mean multiple `memory/orgs/<slug>.md` files later
- **Web UI / mobile app**: not even on the roadmap

---

## 15. Things to NOT carry over from the source project

- Real correspondence, attachments, or state files (PII)
- Plaintext credentials files (replaced by keyring + config.toml)
- Memory content with real names: keep only the **structure** as `examples/memory/` template
- All `.log`, `*_index.json`, `seen_*.json` (state files)
- Hardcoded org-specific paths in error messages
- OS-specific launchers (replaced with cross-platform installer)
- The original task system (replaced by drafts-as-queue)

---

## 16. Open questions / future work

- **MCP server**: design to allow a future MCP layer (`/jarlis search emails about X` from Claude Code natively)
- **Multi-language voice exemplars**: how many per language? what if user writes in 4 languages?
- **Bootstrap quality on small mailboxes**: <50 emails may not give enough signal: need a graceful "not enough data, ask user manually" branch
- **Test fixtures**: need anonymized sample IMAP corpus for tests: generate synthetic, don't ship real emails
- **CI**: GitHub Actions running tests on Linux/macOS/Windows; lint with `ruff`
- **Versioning + changelog**: TBD; semver from 0.1.0

---

## 17. Provenance: what changed from the source project

JARLIS is the generic, publishable form of an internal pipeline. The notable changes vs. that origin:

| Concern | Source project | JARLIS |
|---|---|---|
| Credentials | plaintext config file | OS keychain via `keyring`, with plaintext fallback |
| AI backend | one CLI only | Protocol-based abstraction over claude / codex / gemini / llm |
| Triage | per-email task folders + reminders | three-bucket triage with drafts-as-queue |
| i18n | hardcoded strings in the user's native language | TOML files per language |
| Platform | OS-specific launchers | cross-platform scheduler installer |
| Setup | manual editing of a JSON file | interactive `python -m jarlis.setup` script that never exposes secrets to AI agents |
