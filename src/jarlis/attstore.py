"""Content-addressed attachment store.

Attachments used to be written once per email. The same 13 MB contract
forwarded three times cost 39 MB on disk, and each copy was extracted to
text separately.

Here every attachment is written once under ``email/attachments/_store/``,
named by the SHA-256 of its bytes, and each email folder gets a **hard
link** to it. Readers see exactly what they saw before: a real file at
``email/attachments/<folder>/<name>``, same path, same content, no symlink
to resolve and nothing to teach the rest of the codebase. The filesystem
counts the links, so deleting one email's folder never takes the bytes
away from another email that shares them.

Text extraction follows the bytes: ``<sha>.<ext>.extracted.txt`` lives in
the store too, so the same document is parsed once no matter how many
times it lands in the mailbox.

Falls back to a plain copy when hard links are unavailable (a store and a
mailbox on different filesystems, an exotic mount): the layout is then
exactly the old one, minus the dedup.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import attachments as att_extract
from .config import Config

log = logging.getLogger(__name__)

STORE_DIRNAME = "_store"

# Read size when hashing existing files.
_CHUNK = 1 << 20


def store_dir(cfg: Config) -> Path:
    return cfg.attachments_dir / STORE_DIRNAME


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _extension(filename: str) -> str:
    """Last extension of ``filename``, lowercased, ASCII, or empty."""
    name = (filename or "").strip().lower()
    if "." not in name:
        return ""
    ext = name.rpartition(".")[2]
    ext = "".join(c for c in ext if c.isalnum())
    return ext[:12]


def store_path_for(cfg: Config, digest: str, filename: str) -> Path:
    """Where ``digest`` lives in the store. Sharded to keep directories small."""
    ext = _extension(filename)
    stem = f"{digest}.{ext}" if ext else digest
    return store_dir(cfg) / digest[:2] / stem


def link(src: Path, dest: Path) -> str:
    """Hard-link ``src`` to ``dest``; copy if that fails. Returns what was done."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            if dest.samefile(src):
                return "already-linked"
        except OSError:
            pass
        try:
            dest.unlink()
        except OSError as exc:
            log.warning("could not replace %s: %s", dest, exc)
            return "failed"
    try:
        os.link(src, dest)
        return "linked"
    except OSError as exc:
        log.debug("hard link %s -> %s failed (%s); copying", src, dest, exc)
    try:
        dest.write_bytes(src.read_bytes())
        return "copied"
    except OSError as exc:
        log.warning("could not copy %s -> %s: %s", src, dest, exc)
        return "failed"


def save_attachment(cfg: Config, folder_name: str, filename: str, data: bytes) -> Path:
    """Store ``data`` once and expose it at ``attachments/<folder>/<filename>``.

    Returns the per-email path, which is what the rest of the pipeline uses.
    """
    digest = sha256_bytes(data)
    canonical = store_path_for(cfg, digest, filename)
    if not canonical.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(data)
        try:
            att_extract.extract_and_save(canonical)
        except Exception as exc:
            log.warning("attachment extract failed for %s: %s", canonical, exc)

    dest = cfg.attachments_dir / folder_name / filename
    link(canonical, dest)

    sibling = canonical.with_name(canonical.name + att_extract.EXTRACTED_SUFFIX)
    if sibling.exists():
        link(sibling, dest.with_name(dest.name + att_extract.EXTRACTED_SUFFIX))
    return dest


# ---------- migration / maintenance --------------------------------------


@dataclass
class DedupReport:
    scanned: int = 0
    stored: int = 0
    linked: int = 0
    already_linked: int = 0
    bytes_saved: int = 0
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "stored": self.stored,
            "linked": self.linked,
            "already_linked": self.already_linked,
            "bytes_saved": self.bytes_saved,
            "failures": self.failures[:20],
        }


def _iter_email_attachments(cfg: Config):
    root = cfg.attachments_dir
    if not root.exists():
        return
    store = store_dir(cfg)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if store in path.parents:
                continue
        except (OSError, ValueError):
            continue
        yield path


def dedup_existing(cfg: Config, *, dry_run: bool = False) -> DedupReport:
    """Fold every attachment already on disk into the store.

    Idempotent: files that already point at their store entry are counted
    and skipped. Safe to run on a live mailbox, since replacing a file by a
    hard link to identical bytes is invisible to every reader.
    """
    report = DedupReport()
    # Digests seen during this pass. A dry run writes nothing, so without
    # this the second copy of a file would find an empty store and count as
    # a new one: the report would show no duplicates at all.
    seen: set[str] = set()
    for path in _iter_email_attachments(cfg):
        report.scanned += 1
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError as exc:
            report.failures.append(f"{path}: {exc}")
            continue

        canonical = store_path_for(cfg, digest, path.name)
        if canonical.exists() or digest in seen:
            if canonical.exists():
                try:
                    if path.samefile(canonical):
                        report.already_linked += 1
                        continue
                except OSError:
                    pass
            if dry_run:
                report.linked += 1
                report.bytes_saved += size
                continue
            result = link(canonical, path)
            if result in ("linked", "already-linked"):
                report.linked += 1
                report.bytes_saved += size
            elif result == "copied":
                report.failures.append(f"{path}: hard links unavailable, left a copy")
            else:
                report.failures.append(f"{path}: relink failed")
            continue

        # First time we see these bytes: the store takes ownership of the
        # file itself, then the email folder links back to it.
        report.stored += 1
        seen.add(digest)
        if dry_run:
            continue
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, canonical)
        except OSError:
            try:
                canonical.write_bytes(path.read_bytes())
            except OSError as exc:
                report.failures.append(f"{path}: {exc}")
                continue
            link(canonical, path)
    return report


def gc(cfg: Config, *, dry_run: bool = False) -> tuple[int, int]:
    """Drop store entries nothing links to any more. Returns (files, bytes)."""
    removed = 0
    freed = 0
    store = store_dir(cfg)
    if not store.exists():
        return (0, 0)
    for path in sorted(store.rglob("*")):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_nlink > 1:
            continue
        removed += 1
        freed += st.st_size
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    return (removed, freed)


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json
    import logging as _log
    import sys

    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m jarlis.attstore")
    parser.add_argument("--dry-run", action="store_true", help="report without touching anything")
    parser.add_argument("--gc", action="store_true", help="also drop unreferenced store entries")
    args = parser.parse_args(argv)

    _log.basicConfig(level=_log.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    cfg = load_config()
    report = dedup_existing(cfg, dry_run=args.dry_run)
    out = report.to_dict()
    if args.gc:
        files, freed = gc(cfg, dry_run=args.dry_run)
        out["gc_files"] = files
        out["gc_bytes"] = freed
    print(_json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
