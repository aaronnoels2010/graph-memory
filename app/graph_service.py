"""High-level, token-efficient query API used by the MCP server.

Every method returns plain dicts/lists (not dataclasses) shaped for an LLM:
short, structured, file:line anchored, and size-capped. The service owns the
GraphDB and the indexing entrypoint so the MCP layer stays a thin adapter.
"""
from __future__ import annotations

from pathlib import Path

from . import gitinfo
from .config import Settings, get_settings
from .db import GraphDB
from .exceptions import GitUnavailableError
from .indexer import index_codebase, scan_changes
from .logging_config import configure_logging
from .models import Edge, Symbol

# Hard caps so a single tool call can never dump an unbounded blob into context.
MAX_RESULTS = 100

# Directory names and filename shapes that mark a file as tests, across the
# supported languages' conventions.
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}


def _is_test_file(path: str) -> bool:
    if not path:
        return False
    parts = path.replace("\\", "/").split("/")
    if any(seg.lower() in _TEST_DIRS for seg in parts[:-1]):
        return True
    base = parts[-1]
    stem = base.split(".")[0]
    low = stem.lower()
    lowbase = base.lower()
    return (
        low.startswith("test_") or low.endswith("_test") or low.endswith("_tests")
        or ".test." in lowbase or ".spec." in lowbase
        or stem.endswith("Test") or stem.endswith("Tests") or stem.endswith("Spec")
    )


class GraphService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level)
        self.root = self.settings.root_path.resolve()
        self.db = GraphDB(self.settings.db_path_for(self.root))

    def _ensure_root(self, root: Path) -> None:
        """Point the service at ``root``, opening that repo's DB if it changed."""
        root = root.resolve()
        if root != self.root:
            self.db.close()
            self.root = root
            self.db = GraphDB(self.settings.db_path_for(root))

    # --- indexing ------------------------------------------------------------
    def index(self, path: str | None = None, languages: list[str] | None = None,
              force_full: bool = False) -> dict:
        self._ensure_root(Path(path) if path else self.root)
        return index_codebase(
            self.db, self.settings, root=self.root, languages=languages, force_full=force_full
        )

    # --- lookups -------------------------------------------------------------
    def find_symbol(self, name: str, kind: str | None = None) -> dict:
        symbols = self.db.find_symbol(name, kind)
        return {
            "query": name,
            "count": len(symbols),
            "ambiguous": len(symbols) > 1,
            "symbols": [self._symbol_dict(s) for s in symbols[:MAX_RESULTS]],
        }

    def references(self, symbol: str, limit: int = MAX_RESULTS) -> dict:
        return self._edge_view(symbol, self.db.references, "references", limit, by="src")

    def callers(self, symbol: str, limit: int = MAX_RESULTS) -> dict:
        return self._edge_view(symbol, self.db.callers, "callers", limit, by="src")

    def callees(self, symbol: str, limit: int = MAX_RESULTS) -> dict:
        return self._edge_view(symbol, self.db.callees, "callees", limit, by="dst")

    def blast_radius(self, symbol: str, max_depth: int = 2, resolved_only: bool = False) -> dict:
        resolved = self._resolve_one(symbol)
        if "error" in resolved:
            return resolved
        sid = resolved["symbol"]["id"]
        depth = max(1, min(max_depth, 6))
        pairs = self.db.blast_radius(sid, max_depth=depth, resolved_only=resolved_only)

        by_file = self._group_affected(pairs[:MAX_RESULTS])
        return {
            "symbol": resolved["symbol"],
            "max_depth": depth,
            "resolved_only": resolved_only,
            "affected_count": len(pairs),
            "heuristic_count": sum(1 for _, _, via in pairs if via == "heuristic"),
            "truncated": len(pairs) > MAX_RESULTS,
            "files_affected": len(by_file),
            "by_file": by_file,
        }

    def affected_tests(self, symbol: str, max_depth: int = 3) -> dict:
        """Tests that exercise ``symbol`` (directly or transitively).

        The dependents of a symbol that live in test files are exactly the tests
        worth re-running after changing it — a cheap, high-signal slice of the
        blast radius. Test files are detected by path convention (``tests/``,
        ``test_*``, ``*_test``, ``*.spec.*``, ``FooTest``...).
        """
        resolved = self._resolve_one(symbol)
        if "error" in resolved:
            return resolved
        sid = resolved["symbol"]["id"]
        depth = max(1, min(max_depth, 6))
        pairs = self.db.blast_radius(sid, max_depth=depth)
        files = self.db.symbol_files(affected_id for affected_id, _, _ in pairs)
        test_pairs = [
            (aid, d, via) for (aid, d, via) in pairs if _is_test_file(files.get(aid, ""))
        ]
        by_file = self._group_affected(test_pairs[:MAX_RESULTS])
        return {
            "symbol": resolved["symbol"],
            "max_depth": depth,
            "test_count": len(test_pairs),
            "test_files": sorted(by_file),
            "truncated": len(test_pairs) > MAX_RESULTS,
            "by_file": by_file,
        }

    def path_between(self, src: str, dst: str, max_depth: int = 6) -> dict:
        """Find a directed dependency path ``src -> ... -> dst`` (a call chain)."""
        src_resolved = self._resolve_one(src)
        if "error" in src_resolved:
            return src_resolved
        dst_resolved = self._resolve_one(dst)
        if "error" in dst_resolved:
            return dst_resolved
        max_depth = max(1, min(max_depth, 12))
        path_ids = self.db.path_between(
            src_resolved["symbol"]["id"], dst_resolved["symbol"]["id"], max_depth
        )
        if not path_ids:
            return {
                "src": src_resolved["symbol"],
                "dst": dst_resolved["symbol"],
                "found": False,
                "max_depth": max_depth,
            }
        symbols = self.db.get_symbols(path_ids)
        path = [
            {"id": sid, "qualname": s.qualname, "kind": s.kind,
             "file": s.file, "line": s.start_line}
            for sid in path_ids
            if (s := symbols.get(sid)) is not None
        ]
        return {
            "src": src_resolved["symbol"],
            "dst": dst_resolved["symbol"],
            "found": True,
            "hops": len(path) - 1,
            "path": path,
        }

    def diff_blast_radius(self, files: list[str] | None = None, max_depth: int = 2,
                          resolved_only: bool = False) -> dict:
        """Combined blast radius of changed files (explicit or from ``git diff``)."""
        if files is None:
            if not gitinfo.is_git_repo(self.root):
                return {"error": f"{self.root} is not a git work tree; pass `files` explicitly."}
            try:
                files = gitinfo.changed_files(self.root)
            except GitUnavailableError as exc:
                return {"error": str(exc)}

        known = self.db.known_files()
        changed = [f for f in files if f in known]
        unknown = [f for f in files if f not in known]
        if not changed:
            return {
                "changed_files": [],
                "unknown_files": unknown,
                "affected_count": 0,
                "files_affected": 0,
                "by_file": {},
                "note": "No changed files are in the index. Reindex, or check paths.",
            }

        max_depth = max(1, min(max_depth, 6))
        pairs = self.db.blast_radius_for_files(
            changed, max_depth=max_depth, resolved_only=resolved_only
        )
        by_file = self._group_affected(pairs[:MAX_RESULTS])
        return {
            "changed_files": changed,
            "unknown_files": unknown,
            "max_depth": max_depth,
            "resolved_only": resolved_only,
            "affected_count": len(pairs),
            "heuristic_count": sum(1 for _, _, via in pairs if via == "heuristic"),
            "truncated": len(pairs) > MAX_RESULTS,
            "files_affected": len(by_file),
            "by_file": by_file,
        }

    def fragility(self, limit: int = 20, max_commits: int | None = None) -> dict:
        """Rank files by fragility = git churn x how depended-upon they are.

        Files that change often AND are widely depended-upon are the riskiest to
        touch. Both components are returned so the score stays explainable.
        """
        if not gitinfo.is_git_repo(self.root):
            return {"error": f"{self.root} is not a git work tree; fragility needs git history."}
        try:
            churn = gitinfo.churn(self.root, max_commits=max_commits)
        except GitUnavailableError as exc:
            return {"error": str(exc)}

        dependents = self.db.file_dependents()
        sym_counts = self.db.symbol_counts_by_file()

        ranked = []
        for file, indeg in dependents.items():
            commits = churn.get(file, 0)
            score = commits * indeg
            if score == 0:
                continue
            ranked.append({
                "file": file,
                "score": score,
                "churn_commits": commits,
                "dependents": indeg,
                "symbols": sym_counts.get(file, 0),
            })
        ranked.sort(key=lambda r: (-r["score"], r["file"]))
        limit = max(1, min(limit, MAX_RESULTS))
        return {
            "scored_files": len(ranked),
            "limit": limit,
            "fragile": ranked[:limit],
        }

    def file_outline(self, path: str) -> dict:
        symbols = self.db.file_outline(path)
        return {
            "file": path,
            "count": len(symbols),
            "symbols": [self._symbol_dict(s) for s in symbols[:MAX_RESULTS]],
        }

    def stats(self) -> dict:
        return {**self.db.stats(), "resolution": self.db.resolution_stats()}

    def status(self, path: str | None = None) -> dict:
        """Report whether the on-disk graph is current.

        Compares indexed files against what's on disk (cheaply: by mtime, falling
        back to a content hash only when mtime differs) so an agent knows when to
        reindex before trusting a blast radius. Capped lists keep the response
        small even when many files changed.
        """
        self._ensure_root(Path(path) if path else self.root)
        base = self.db.stats()
        if base["files"] == 0:
            return {"root": str(self.root), "indexed": False, "stale": True, **base}
        changes = scan_changes(self.db, self.settings, root=self.root)
        stale = bool(changes["changed"] or changes["added"] or changes["removed"])
        return {
            "root": str(self.root),
            "indexed": True,
            "stale": stale,
            "changed_files": changes["changed"][:MAX_RESULTS],
            "added_files": changes["added"][:MAX_RESULTS],
            "removed_files": changes["removed"][:MAX_RESULTS],
            "changed_count": len(changes["changed"]),
            "added_count": len(changes["added"]),
            "removed_count": len(changes["removed"]),
            **base,
        }

    # --- internals -----------------------------------------------------------
    def _resolve_one(self, ref: str) -> dict:
        """Resolve a ref to a single symbol, or return an error/ambiguity dict."""
        matches = self.db.find_symbol(ref)
        if not matches:
            return {"error": f"No symbol matching {ref!r}. Has the codebase been indexed?"}
        if len(matches) > 1:
            return {
                "error": f"{ref!r} is ambiguous ({len(matches)} matches); pass a full id.",
                "candidates": [self._symbol_dict(s) for s in matches[:MAX_RESULTS]],
            }
        return {"symbol": self._symbol_dict(matches[0])}

    def _edge_view(self, ref: str, fetch, label: str, limit: int, *, by: str) -> dict:
        resolved = self._resolve_one(ref)
        if "error" in resolved:
            return resolved
        sid = resolved["symbol"]["id"]
        limit = max(1, min(limit, MAX_RESULTS))
        edges: list[Edge] = fetch(sid)
        shown = edges[:limit]
        symbols = self.db.get_symbols(
            (e.dst_symbol if by == "dst" else e.src_symbol) for e in shown
        )
        items = []
        for e in shown:
            other_id = e.dst_symbol if by == "dst" else e.src_symbol
            sym = symbols.get(other_id)
            if sym is None:
                continue
            items.append({
                "id": sym.id,
                "qualname": sym.qualname,
                "kind": sym.kind,
                "file": sym.file,
                "line": sym.start_line,
                "type": e.type,
                "resolution": e.resolution,
                "count": e.count,
            })
        return {
            "symbol": resolved["symbol"],
            label: items,
            "count": len(edges),
            "truncated": len(edges) > limit,
        }

    def _group_affected(self, pairs: list[tuple[str, int, str]]) -> dict[str, list[dict]]:
        """Group (symbol_id, depth, reached_via) triples by file into a compact,
        scannable view. ``reached_via`` tells whether a symbol's inclusion rests
        on a guessed (heuristic) edge anywhere on its shortest path."""
        symbols = self.db.get_symbols(affected_id for affected_id, _, _ in pairs)
        by_file: dict[str, list[dict]] = {}
        for affected_id, depth, reached_via in pairs:
            sym = symbols.get(affected_id)
            if sym is None:
                continue
            by_file.setdefault(sym.file, []).append(
                {"id": sym.id, "qualname": sym.qualname, "kind": sym.kind,
                 "line": sym.start_line, "depth": depth, "reached_via": reached_via}
            )
        return by_file

    @staticmethod
    def _symbol_dict(s: Symbol) -> dict:
        return {
            "id": s.id,
            "name": s.name,
            "qualname": s.qualname,
            "kind": s.kind,
            "file": s.file,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "signature": s.signature,
        }

    def close(self) -> None:
        self.db.close()
