"""Repo walk + incremental (re)indexing.

Only changed files are re-parsed: each file is hashed (content sha1) and skipped
when its hash matches what's stored. After parsing, edges are rebuilt globally
from the occurrences table so cross-file references stay correct even when only
one file changed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import DEFAULT_IGNORE_DIRS, Settings
from .db import GraphDB
from .languages import language_for_path
from .logging_config import get_logger
from .parser import parse_source

logger = get_logger(__name__)


def _iter_source_files(root: Path, languages: set[str], max_bytes: int):
    """Yield (relpath, abspath, lang) for indexable files under root.

    Approximates .gitignore: skips DEFAULT_IGNORE_DIRS and any dot-directory.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(p in DEFAULT_IGNORE_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        rel = path.relative_to(root).as_posix()
        lang = language_for_path(rel)
        if lang is None or lang not in languages:
            continue
        try:
            if path.stat().st_size > max_bytes:
                logger.debug("skipping large file %s", rel)
                continue
        except OSError:
            continue
        yield rel, path, lang


def index_codebase(
    db: GraphDB,
    settings: Settings,
    *,
    root: Path | None = None,
    languages: list[str] | None = None,
    force_full: bool = False,
) -> dict:
    """(Re)index a codebase into the graph. Returns a summary dict."""
    root = (root or settings.root_path).resolve()
    langs = set(languages or settings.languages)
    if not root.is_dir():
        raise NotADirectoryError(f"root path is not a directory: {root}")

    stored_hashes = {} if force_full else db.get_file_hashes()
    seen: set[str] = set()
    parsed = skipped = failed = 0
    failures: list[str] = []

    for rel, abspath, lang in _iter_source_files(root, langs, settings.max_file_bytes):
        seen.add(rel)
        try:
            source = abspath.read_bytes()
        except OSError as exc:
            logger.warning("could not read %s: %s", rel, exc)
            failed += 1
            failures.append(rel)
            continue

        file_hash = hashlib.sha1(source).hexdigest()
        if not force_full and stored_hashes.get(rel) == file_hash:
            skipped += 1
            continue

        try:
            result = parse_source(rel, source, lang)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the index
            logger.warning("failed to parse %s: %s", rel, exc)
            failed += 1
            failures.append(rel)
            continue

        db.replace_file(
            path=rel,
            lang=lang,
            file_hash=file_hash,
            mtime=abspath.stat().st_mtime,
            symbols=result.symbols,
            occurrences=result.occurrences,
        )
        parsed += 1

    # Drop files that vanished since the last index.
    removed = db.known_files() - seen
    if removed:
        db.remove_files(removed)

    edges = db.rebuild_edges()
    summary = {
        "root": str(root),
        "parsed_files": parsed,
        "skipped_files": skipped,
        "removed_files": len(removed),
        "failed_files": failed,
        "edges": edges,
        **db.stats(),
    }
    if failures:
        summary["failures"] = failures[:20]
    logger.info(
        "indexed %s: parsed=%d skipped=%d removed=%d failed=%d edges=%d",
        root, parsed, skipped, len(removed), failed, edges,
    )
    return summary
