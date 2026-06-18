"""Read-only git introspection: changed files and per-file churn.

Git is invoked as an external process in *read-only* mode (``diff``/``log``),
never mutating the repo and never importing or running the target code — same
zero-execution contract as the parser. Every call is bounded by ``_TIMEOUT`` so a
pathological repo can't hang the MCP server.

All helpers raise :class:`GitUnavailableError` when git is missing or the root is
not inside a work tree, so callers can degrade to a friendly message.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from .exceptions import GitUnavailableError
from .logging_config import get_logger

logger = get_logger(__name__)

_TIMEOUT = 15  # seconds; generous for git log on a large history, bounded enough


def _run(root: Path, *args: str) -> str:
    """Run ``git -C root <args>`` and return stdout, or raise GitUnavailableError."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitUnavailableError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailableError(f"git timed out after {_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise GitUnavailableError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or 'unknown error'}"
        )
    return proc.stdout


def is_git_repo(root: Path) -> bool:
    try:
        return _run(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitUnavailableError:
        return False


def changed_files(root: Path) -> list[str]:
    """Repo-relative paths changed vs HEAD (staged + unstaged) plus untracked.

    This is "what have I touched since the last commit" — the natural seed set
    for a diff-aware blast radius.
    """
    tracked = _run(root, "diff", "--name-only", "HEAD")
    untracked = _run(root, "ls-files", "--others", "--exclude-standard")
    paths = {line.strip() for line in (tracked + "\n" + untracked).splitlines()}
    return sorted(p for p in paths if p)


def churn(root: Path, max_commits: int | None = None) -> dict[str, int]:
    """Map repo-relative path -> number of commits that touched it.

    A simple, explainable churn metric: files that change often are riskier to
    depend on. ``max_commits`` bounds history for very large repos.
    """
    args = ["log", "--name-only", "--format=", "--no-merges"]
    if max_commits:
        args.append(f"-n{max_commits}")
    counts: Counter[str] = Counter()
    for line in _run(root, *args).splitlines():
        line = line.strip()
        if line:
            counts[line] += 1
    return dict(counts)
