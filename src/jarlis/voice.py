"""Voice exemplar selection.

Picks a small, diverse set of the user's own sent emails per language and
stores them as voice exemplars under ``memory/voice/<lang>/``. These are
injected as in-context examples at draft time so the model copies the
user's actual phrasing.

Language detection has two layers:

  1. ``langdetect`` (bundled dependency). Covers ~55 languages and returns
     ISO 639-1 codes. This is the primary detector and should handle
     anything the user actually receives.
  2. A built-in keyword heuristic recognizing ja/zh/ko/ar/he/ru + a few
     romance/germanic markers (en, fr, es, de, it, pt). Used as a fallback
     when langdetect import fails (degraded environments) or returns nothing
     useful on very short text.

Either way, the result is a short language code (``en``, ``fr``, ``ja``,
``es``, ...) used to group voice exemplars and to tell the AI "reply in X".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from . import memory
from .config import Config
from .models import Email

DEFAULT_PER_LANG = 5
MIN_BODY_CHARS = 80
# Loosened from 1500 → 4000 because real work emails often run several
# thousand chars (technical updates, grant applications, policy
# discussions), not just one-liners.
MAX_BODY_CHARS = 4000
# Loosened from 5 → 15 to admit bureau / committee threads where every
# member is in To/Cc but the email is still substantive personal voice.
MAX_RECIPIENTS = 15


# langdetect is a pinned dependency in pyproject.toml. The try/except here
# is purely defensive for degraded installs (e.g. air-gapped, broken pip,
# manual subset deployments). The heuristic below is the fallback.
try:
    from langdetect import DetectorFactory, detect as _ld_detect  # type: ignore[import-not-found]

    DetectorFactory.seed = 0  # deterministic results across runs
    _HAS_LANGDETECT = True
except ImportError:  # pragma: no cover, environment-dependent
    _ld_detect = None  # type: ignore[assignment]
    _HAS_LANGDETECT = False


# Heuristic markers per language. Order is checked top-down; first match wins
# for the script-based languages (CJK, Cyrillic, Arabic, Hebrew). For
# Latin-script languages we use keyword voting and pick the highest scoring.
_SCRIPT_RANGES: tuple[tuple[str, str, str], ...] = (
    ("ja", "぀", "ヿ"),       # Hiragana + Katakana
    ("ja", "一", "鿿"),       # CJK unified: covers ja kanji + zh hanzi
    ("ko", "가", "힣"),       # Hangul syllables
    ("ru", "А", "я"),         # Cyrillic
    ("ar", "؀", "ۿ"),         # Arabic
    ("he", "֐", "׿"),         # Hebrew
)

_LATIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fr": ("bonjour", "cordialement", "bien à", "merci", "à vous", "pour vous",
           "avec vous", "amicalement", "salutations", "veuillez", "bonne journée"),
    "es": ("hola", "saludos", "gracias", "atentamente", "buenos días",
           "buenas tardes", "cordialmente", "estimad", "por favor"),
    "de": ("hallo", "guten tag", "grüße", "mit freundlichen", "danke",
           "viele grüße", "sehr geehrte", "bitte"),
    "it": ("buongiorno", "saluti", "grazie", "cordiali saluti",
           "distinti saluti", "gentile", "per favore"),
    "pt": ("olá", "obrigado", "obrigada", "saudações", "atenciosamente",
           "cumprimentos", "por favor"),
    "en": ("hello", "hi ", "thanks", "thank you", "regards", "best,",
           "cheers", "kind regards", "looking forward", "please"),
}


def _heuristic_detect(text: str) -> str:
    """Keyword + script heuristic. Returns an ISO 639-1 short code; default 'en'."""
    if not text:
        return "en"

    # Script-based detection: highest-frequency wins.
    counts: dict[str, int] = defaultdict(int)
    total = max(len(text), 1)
    for lang, lo, hi in _SCRIPT_RANGES:
        n = sum(1 for c in text if lo <= c <= hi)
        if n / total > 0.05:
            counts[lang] += n
    if counts:
        # ja-vs-zh tie-break: hiragana/katakana presence locks ja.
        if "ja" in counts:
            return "ja"
        return max(counts.items(), key=lambda kv: kv[1])[0]

    # Latin-script keyword voting.
    lower = text.lower()
    scores: dict[str, int] = {}
    for lang, kws in _LATIN_KEYWORDS.items():
        scores[lang] = sum(1 for kw in kws if kw in lower)
    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score >= 2:
        return best_lang
    return "en"


def detect_language(text: str) -> str:
    """Best-effort language detection. Returns an ISO 639-1 short code.

    Uses ``langdetect`` if available (covers ~55 languages); falls back to
    the built-in heuristic. The heuristic distinguishes en, fr, es, de, it,
    pt, ja, zh-via-CJK, ko, ru, ar, he, with English as the safe default.
    """
    if not text:
        return "en"
    if _HAS_LANGDETECT and _ld_detect is not None:
        try:
            code = _ld_detect(text)
            # langdetect returns codes like 'zh-cn'; normalize to short form.
            if code:
                return code.split("-")[0]
        except Exception:
            pass
    return _heuristic_detect(text)


def _looks_like_jarlis_notification(
    email_obj: Email,
    *,
    notify_to: str = "",
) -> bool:
    """True if this looks like an email JARLIS itself generated.

    JARLIS-generated emails (recap, stuck-pipeline alert, draft proposal,
    test ping) live in your Sent folder once delivered, and would otherwise
    pollute the voice corpus with JARLIS's own boilerplate. We detect them
    via two cheap heuristics:

      1. ``To:`` is the notification address AND the subject starts with
         the JARLIS ``[<org>]`` bracket prefix.
      2. The body ends with the literal ``JARLIS`` signature line.
    """
    body = (email_obj.body_text or "").rstrip()
    if body.endswith("JARLIS") and len(body) < 4000:
        # The trailing 'JARLIS' or '\nJARLIS' line is the canonical signoff
        # in every i18n notification template. Combined with a short body it's
        # unambiguous.
        last_line = body.rsplit("\n", 1)[-1].strip()
        if last_line == "JARLIS":
            return True
    if notify_to:
        recipients = {(r or "").lower() for r in (email_obj.to or [])}
        if (notify_to or "").lower() in recipients:
            subject = (email_obj.subject or "").strip()
            # Subjects of JARLIS notifications start with "[<org>] " followed
            # by one of the localized prefixes. We don't list them all; the
            # combination of "to=notification" + bracket-prefix is enough.
            if subject.startswith("["):
                return True
    return False


def select_exemplars(
    sent_emails: Sequence[Email],
    *,
    per_lang: int = DEFAULT_PER_LANG,
    notify_to: str = "",
) -> dict[str, list[Email]]:
    """Pick up to ``per_lang`` diverse exemplars per detected language.

    Heuristics, applied in order:
      - skip bodies outside [MIN_BODY_CHARS, MAX_BODY_CHARS]
      - skip emails with > MAX_RECIPIENTS in To: (likely list mail)
      - de-dup by first-100-char prefix (same boilerplate fired twice = one exemplar)
      - prefer different correspondents over repeated exchanges with one person
    """
    out: dict[str, list[Email]] = defaultdict(list)
    seen_prefixes: set[str] = set()

    def _key(e: Email):
        return e.date or datetime.min

    for email_obj in sorted(sent_emails, key=_key, reverse=True):
        # Drop JARLIS's own outgoing notifications first; otherwise the test
        # ping and recap emails would be picked up as 'user voice'.
        if _looks_like_jarlis_notification(email_obj, notify_to=notify_to):
            continue
        body = (email_obj.body_text or "").strip()
        n = len(body)
        if not (MIN_BODY_CHARS <= n <= MAX_BODY_CHARS):
            continue
        if len(email_obj.to or []) > MAX_RECIPIENTS:
            continue
        # Prefix dedup keeps boilerplate templates from filling the corpus
        # with copies of the same opening. Per-recipient dedup was removed:
        # if the user writes regularly to one person across topics, those
        # different topics are still useful style data.
        prefix = body[:100]
        if prefix in seen_prefixes:
            continue

        lang = detect_language(body)
        if len(out[lang]) >= per_lang:
            continue

        out[lang].append(email_obj)
        seen_prefixes.add(prefix)

    return dict(out)


def render_exemplar(email_obj: Email) -> str:
    """Format an exemplar as markdown for memory/voice/."""
    subject = email_obj.subject or "(no subject)"
    lines = [
        f"# Voice exemplar: {subject}",
        "",
        f"> Subject: {subject}",
        ">",
    ]
    body = (email_obj.body_text or "").strip()
    for body_line in body.splitlines():
        lines.append(f"> {body_line}")
    return "\n".join(lines) + "\n"


def save_exemplars(cfg: Config, by_lang: dict[str, list[Email]]) -> dict[str, list[str]]:
    """Persist exemplars under ``memory/voice/<lang>/exemplar_NN.md``.

    Returns ``{lang: [relative_paths]}`` for the report shown to the user.
    """
    paths_by_lang: dict[str, list[str]] = {}
    for lang, emails in by_lang.items():
        rel: list[str] = []
        for i, e in enumerate(emails, start=1):
            path = memory.save_voice_exemplar(cfg, lang, i, render_exemplar(e))
            rel.append(str(path.relative_to(cfg.memory_dir)))
        paths_by_lang[lang] = rel
    return paths_by_lang
