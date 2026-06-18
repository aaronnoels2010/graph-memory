"""Plain dataclasses shared across the parser, store, and service.

Kept dependency-free (no pydantic) because these are internal value objects; the
MCP layer serialises them to dicts itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Symbol kinds we recognise. "module" is a synthetic per-file symbol that owns
# top-level (module-scope) call/reference occurrences.
SYMBOL_KINDS = ("module", "class", "function", "method")

# Occurrence / edge relationship types.
#   call      - a function/method invocation
#   base      - a class names another as a base (inherits)
#   import    - an imported name (mostly resolves to external; see resolver)
OCCURRENCE_TYPES = ("call", "base", "import")


@dataclass
class Symbol:
    """A definition site: a module, class, function, or method."""

    id: str            # stable: f"{relpath}::{qualname}"
    name: str          # bare name, e.g. "greet"
    qualname: str      # scoped name, e.g. "Greeter.hello"
    kind: str          # one of SYMBOL_KINDS
    file: str          # repo-relative path
    start_line: int    # 1-based
    end_line: int      # 1-based
    signature: str     # first line of the definition, truncated


@dataclass
class Occurrence:
    """A raw, unresolved reference found inside a symbol's body.

    Persisted so that edge resolution can be re-run globally after an
    incremental re-parse without touching unchanged files.
    """

    file: str
    src_symbol: str    # enclosing Symbol.id
    name: str          # the referenced bare name
    type: str          # one of OCCURRENCE_TYPES
    line: int          # 1-based


@dataclass
class Edge:
    """A resolved relationship between two symbols."""

    src_symbol: str
    dst_symbol: str
    dst_name: str
    type: str              # one of OCCURRENCE_TYPES
    resolution: str        # "resolved" (unique match) | "heuristic" (ambiguous)
    count: int = 1         # how many occurrences collapsed into this edge


@dataclass
class ParseResult:
    symbols: list[Symbol] = field(default_factory=list)
    occurrences: list[Occurrence] = field(default_factory=list)
