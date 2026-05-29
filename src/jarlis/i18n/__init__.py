"""i18n string loader for JARLIS user-facing text.

Strings live in ``<lang>.toml`` next to this module. The loader flattens
nested tables into dotted keys (``notify.stuck.subject``), supports
``str.format`` interpolation, and falls back to English when a key is
missing in the requested language.

Operator-facing logs are NOT translated: they stay in English so support
queries reference common terminology.

## Adding a language

JARLIS ships ``en.toml`` (canonical), ``fr.toml`` and ``ja.toml``. To add
another language:

1. Copy ``en.toml`` to ``<your-iso-code>.toml`` (e.g. ``de.toml``, ``es.toml``).
2. Translate the values, leaving the ``{placeholder}`` interpolation tokens
   intact. Any key you skip falls back to English.
3. Set ``[user].languages`` in ``config.toml`` to ``["<your-code>", ...]``
   so the loader picks it up.

The loader auto-discovers languages via :func:`available_languages`, so no
code change is needed when you add a TOML file.
"""

from __future__ import annotations

import logging
import tomllib
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

_I18N_DIR = Path(__file__).parent
DEFAULT_LANG = "en"


@cache
def _load_raw(lang: str) -> dict:
    path = _I18N_DIR / f"{lang}.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


@cache
def _strings(lang: str) -> dict[str, str]:
    return _flatten(_load_raw(lang))


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Look up ``key`` in the language file; format-interpolate ``kwargs``.

    Falls back to English (``DEFAULT_LANG``) when missing in ``lang``.
    Returns the dotted key itself when missing in both: visible-by-design
    so untranslated strings are obvious in the UI.
    """
    text = _strings(lang).get(key)
    if text is None and lang != DEFAULT_LANG:
        text = _strings(DEFAULT_LANG).get(key)
    if text is None:
        log.warning("missing i18n key: %s (lang=%s)", key, lang)
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError) as exc:
            log.warning("interpolation error for key %s (%s): %s", key, lang, exc)
            return text
    return text


def available_languages() -> list[str]:
    """Return the language codes for which a TOML file exists."""
    return sorted(p.stem for p in _I18N_DIR.glob("*.toml"))


def has_key(key: str, lang: str = DEFAULT_LANG) -> bool:
    """Return whether ``key`` is defined in ``lang`` (no English fallback)."""
    return key in _strings(lang)


@cache
def all_signature_closings() -> frozenset[str]:
    """Aggregate sign-off keywords across all installed language files.

    Each ``<lang>.toml`` may declare a ``[signature].closings = [...]``
    list of lower-cased keywords (e.g. ``cordialement``, ``best regards``).
    The body-signature detector uses the union to recognize Western-style
    email sign-offs in any language we ship a TOML for.

    Adding a new language is a pure data change: drop a new TOML in this
    directory, add its closings under ``[signature]``, and the detector
    picks them up at the next process start (cached for the session).
    """
    out: set[str] = set()
    for lang in available_languages():
        section = _load_raw(lang).get("signature", {})
        for k in section.get("closings", []) or []:
            if isinstance(k, str) and k.strip():
                out.add(k.strip().lower())
    return frozenset(out)
