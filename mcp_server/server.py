"""MCP server exposing the Codebase Knowledge Graph over stdio.

Unlike the persistent-memory project, this server calls the graph engine
*in-process* (no HTTP hop): tree-sitter, sqlite, and the `mcp` SDK have no
dependency conflict, so a single venv and a direct import are simpler.

Run directly for stdio transport:
    python mcp_server/server.py

Configure the codebase to index via env (see app/config.py): ROOT_PATH,
DATA_DIR, LANGUAGES, LOG_LEVEL.
"""
from __future__ import annotations

import os
import sys

# Allow `python mcp_server/server.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from app.graph_service import GraphService  # noqa: E402

INSTRUCTIONS = """\
This server exposes a queryable knowledge graph of a codebase: symbols
(modules, classes, functions, methods) and the relationships between them
(calls, references, inheritance).

Use it to answer STRUCTURAL questions without reading whole files:
  - "where is X defined?"            -> find_symbol
  - "who calls / uses X?"            -> get_callers / find_references
  - "what does X call?"              -> get_callees
  - "what breaks if I change X?"     -> blast_radius
  - "what's in this file?"           -> get_file_outline

Call index_codebase once per session (or after large external changes) before
querying. Results are static analysis: dynamically dispatched or duck-typed
calls may be marked "heuristic" or missed entirely — treat edges as strong hints,
not proof.
"""

mcp = FastMCP("codebase-graph", instructions=INSTRUCTIONS)
_service: GraphService | None = None


def service() -> GraphService:
    global _service
    if _service is None:
        _service = GraphService()
    return _service


@mcp.tool()
def index_codebase(path: str | None = None, languages: list[str] | None = None,
                   force_full: bool = False) -> dict:
    """Build or refresh the graph index for a codebase.

    Only changed files are re-parsed unless force_full=True. Returns counts and
    the resulting graph size.

    Args:
        path: Root directory to index (defaults to the server's ROOT_PATH).
        languages: Subset of languages to index (defaults to all configured).
        force_full: Re-parse every file, ignoring cached hashes.
    """
    return service().index(path=path, languages=languages, force_full=force_full)


@mcp.tool()
def find_symbol(name: str, kind: str | None = None) -> dict:
    """Resolve a name to its definition(s): file, line range, and signature.

    Args:
        name: Bare name ("greet"), qualified name ("Greeter.hello"), or full id.
        kind: Optionally restrict to module | class | function | method.
    """
    return service().find_symbol(name, kind)


@mcp.tool()
def find_references(symbol: str, limit: int = 100) -> dict:
    """List everywhere a symbol is used (any relationship type).

    Args:
        symbol: A name or full symbol id (use the id when find_symbol is ambiguous).
        limit: Max results to return (1–100).
    """
    return service().references(symbol, limit)


@mcp.tool()
def get_callers(symbol: str, limit: int = 100) -> dict:
    """List functions/methods that call the given symbol (one hop up the call graph).

    Args:
        symbol: A name or full symbol id.
        limit: Max results to return (1–100).
    """
    return service().callers(symbol, limit)


@mcp.tool()
def get_callees(symbol: str, limit: int = 100) -> dict:
    """List the symbols that the given symbol calls (one hop down the call graph).

    Args:
        symbol: A name or full symbol id.
        limit: Max results to return (1–100).
    """
    return service().callees(symbol, limit)


@mcp.tool()
def blast_radius(symbol: str, max_depth: int = 2) -> dict:
    """Estimate what breaks if a symbol changes: the transitive set of dependents.

    Walks the call/reference/inheritance graph in reverse, grouped by file.

    Args:
        symbol: A name or full symbol id.
        max_depth: How many hops of dependents to include (1–6).
    """
    return service().blast_radius(symbol, max_depth)


@mcp.tool()
def get_file_outline(path: str) -> dict:
    """List the symbols defined in a file (repo-relative path), in source order.

    Args:
        path: Repo-relative file path, e.g. "app/db.py".
    """
    return service().file_outline(path)


@mcp.tool()
def path_between(src: str, dst: str, max_depth: int = 6) -> dict:
    """Find a directed dependency path (call chain) from one symbol to another.

    Answers "how does src reach dst?" — returns the shortest src -> ... -> dst
    chain following call/reference/inheritance edges, or found=False if none
    exists within max_depth.

    Args:
        src: Starting symbol (name or full id).
        dst: Target symbol (name or full id).
        max_depth: Max chain length to search (1–12).
    """
    return service().path_between(src, dst, max_depth)


@mcp.tool()
def diff_blast_radius(files: list[str] | None = None, max_depth: int = 2) -> dict:
    """Combined blast radius of changed files: what downstream code is affected.

    With no arguments, auto-detects changed files via `git diff` (staged,
    unstaged, and untracked) against HEAD. Ideal before/within a PR or refactor.

    Args:
        files: Repo-relative paths to treat as changed (defaults to git diff).
        max_depth: How many hops of dependents to include (1–6).
    """
    return service().diff_blast_radius(files, max_depth)


@mcp.tool()
def fragility(limit: int = 20, max_commits: int | None = None) -> dict:
    """Rank files by fragility = git churn x how widely they're depended upon.

    Surfaces the riskiest files to change: those edited often AND relied on by
    much other code. Requires the indexed repo to be a git work tree.

    Args:
        limit: Max files to return (1–100).
        max_commits: Bound git history to the most recent N commits (optional).
    """
    return service().fragility(limit, max_commits)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
