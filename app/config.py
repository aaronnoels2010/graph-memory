"""Application configuration, loaded from environment / .env file."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Directories that are never worth indexing. Kept conservative; .gitignore is
# only approximated (see indexer.py) — anything starting with "." is also skipped.
DEFAULT_IGNORE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "site-packages",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Central cache dir holding one DB per indexed repo (keyed by repo path), so
    # the server can be registered once at user scope and used from any repo
    # without polluting it or mixing graphs.
    data_dir: Path = Path.home() / ".graph-memory"

    # The codebase to index when none is passed explicitly. "." resolves to the
    # process's working directory — for a user-scoped MCP server that is the repo
    # Claude Code was launched in.
    root_path: Path = Path(".")

    # Logging
    log_level: str = "INFO"

    # Languages to index (must be registered in languages.py).
    languages: list[str] = ["python", "typescript", "javascript", "java", "csharp", "php"]

    # Skip files larger than this (bytes) — usually generated/minified.
    max_file_bytes: int = 1_000_000

    def db_path_for(self, root: Path) -> Path:
        """Per-repo DB path, keyed by the repo's absolute path.

        Distinct repos get distinct DBs; re-opening the same repo reuses its
        cached graph (and incremental reindex).
        """
        key = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:12]
        return self.data_dir / f"graph-{key}.db"

    @property
    def sqlite_path(self) -> Path:
        return self.db_path_for(self.root_path)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
