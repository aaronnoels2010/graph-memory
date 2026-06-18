"""Domain exceptions."""
from __future__ import annotations


class GraphServiceError(Exception):
    """Base class for service-level errors."""


class ConfigurationError(GraphServiceError):
    """Invalid or incomplete configuration."""


class UnsupportedLanguageError(GraphServiceError):
    """A language was requested that has no registered tree-sitter grammar."""

    def __init__(self, language: str):
        self.language = language
        super().__init__(f"Unsupported language: {language!r}")


class SymbolNotFoundError(GraphServiceError):
    """Requested symbol id / name could not be resolved."""

    def __init__(self, ref: str):
        self.ref = ref
        super().__init__(f"No symbol matching {ref!r}")


class GitUnavailableError(GraphServiceError):
    """Git is not installed, or the indexed root is not inside a git work tree.

    Raised by git-backed features (diff impact, churn/fragility) so the service
    can degrade to a friendly message instead of crashing.
    """
