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
from .indexer import index_codebase
from .logging_config import configure_logging
from .models import Edge, Symbol

# Hard caps so a single tool call can never dump an unbounded blob into context.
MAX_RESULTS = 100


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

    def blast_radius(self, symbol: str, max_depth: int = 2) -> dict:
        resolved = self._resolve_one(symbol)
        if "error" in resolved:
            return resolved
        sid = resolved["symbol"]["id"]
        pairs = self.db.blast_radius(sid, max_depth=max(1, min(max_depth, 6)))

        by_file = self._group_affected(pairs[:MAX_RESULTS])
        return {
            "symbol": resolved["symbol"],
            "max_depth": max(1, min(max_depth, 6)),
            "affected_count": len(pairs),
            "truncated": len(pairs) > MAX_RESULTS,
            "files_affected": len(by_file),
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

    def diff_blast_radius(self, files: list[str] | None = None, max_depth: int = 2) -> dict:
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
        pairs = self.db.blast_radius_for_files(changed, max_depth=max_depth)
        by_file = self._group_affected(pairs[:MAX_RESULTS])
        return {
            "changed_files": changed,
            "unknown_files": unknown,
            "max_depth": max_depth,
            "affected_count": len(pairs),
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
        return self.db.stats()

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

    def _group_affected(self, pairs: list[tuple[str, int]]) -> dict[str, list[dict]]:
        """Group (symbol_id, depth) pairs by file into a compact, scannable view."""
        symbols = self.db.get_symbols(affected_id for affected_id, _ in pairs)
        by_file: dict[str, list[dict]] = {}
        for affected_id, depth in pairs:
            sym = symbols.get(affected_id)
            if sym is None:
                continue
            by_file.setdefault(sym.file, []).append(
                {"id": sym.id, "qualname": sym.qualname, "kind": sym.kind,
                 "line": sym.start_line, "depth": depth}
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
