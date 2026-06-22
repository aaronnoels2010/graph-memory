"""SQLite store for the codebase graph + the graph queries themselves.

Three persisted tables:
  files        - one row per indexed file (hash/mtime drive incremental reindex)
  symbols      - definition sites
  occurrences  - raw, unresolved references (kept so edges can be rebuilt
                 globally after an incremental re-parse without re-reading
                 unchanged files)

`edges` is a derived table, rebuilt from `occurrences` on every index by
resolving reference names against the global symbol table. Resolution is cheap
(name lookups); parsing is the expensive part we make incremental.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Edge, Occurrence, Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    hash        TEXT NOT NULL,
    mtime       REAL NOT NULL,
    lang        TEXT NOT NULL,
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    qualname    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    file        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    signature   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);

CREATE TABLE IF NOT EXISTS occurrences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file        TEXT NOT NULL,
    src_symbol  TEXT NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    line        INTEGER NOT NULL,
    module      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_occ_file ON occurrences(file);
CREATE INDEX IF NOT EXISTS idx_occ_name ON occurrences(name);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_symbol  TEXT NOT NULL,
    dst_symbol  TEXT NOT NULL,
    dst_name    TEXT NOT NULL,
    type        TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_symbol);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _module_matches(module: str, file_path: str) -> bool:
    """True if an import's ``module`` plausibly refers to ``file_path``.

    Best-effort, purely textual: normalise both to dotted form and accept an
    exact match, a suffix match (``pkg.mod`` for ``a/b/pkg/mod.py``), or a
    matching final segment (handles relative imports like ``./mod``). Used only
    to pick between candidates that already share the referenced name, so a loose
    match can at worst leave the edge flagged ``heuristic`` — never invent one.
    """
    mod = module.strip().strip("'\"`").lstrip("./").replace("/", ".").strip(".")
    if not mod:
        return False
    fp = file_path.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
    return fp == mod or fp.endswith("." + mod) or fp.split(".")[-1] == mod.split(".")[-1]


def _row_to_symbol(row: sqlite3.Row) -> Symbol:
    return Symbol(
        id=row["id"],
        name=row["name"],
        qualname=row["qualname"],
        kind=row["kind"],
        file=row["file"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        signature=row["signature"],
    )


class GraphDB:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets reads proceed during a write, and busy_timeout makes a brief
        # lock from a concurrent MCP call wait rather than raise immediately.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older on-disk schema up to date.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so new
        columns must be added explicitly. ``edges`` is derived (rebuilt on every
        index), but we add the column rather than dropping the table so existing
        graphs stay queryable until the next reindex.
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(edges)")}
        if "count" not in cols:
            self._conn.execute("ALTER TABLE edges ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
        occ_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(occurrences)")}
        if "module" not in occ_cols:
            self._conn.execute("ALTER TABLE occurrences ADD COLUMN module TEXT NOT NULL DEFAULT ''")

    # --- file bookkeeping ----------------------------------------------------
    def get_file_hashes(self) -> dict[str, str]:
        return {
            row["path"]: row["hash"]
            for row in self._conn.execute("SELECT path, hash FROM files")
        }

    def get_file_meta(self) -> dict[str, tuple[str, float]]:
        """Map path -> (content hash, mtime) for cheap staleness checks."""
        return {
            row["path"]: (row["hash"], row["mtime"])
            for row in self._conn.execute("SELECT path, hash, mtime FROM files")
        }

    def known_files(self) -> set[str]:
        return {row["path"] for row in self._conn.execute("SELECT path FROM files")}

    def replace_file(
        self, *, path: str, lang: str, file_hash: str, mtime: float,
        symbols: Iterable[Symbol], occurrences: Iterable[Occurrence],
    ) -> None:
        """Atomically replace one file's rows (symbols + occurrences + meta)."""
        cur = self._conn
        cur.execute("DELETE FROM symbols WHERE file = ?", (path,))
        cur.execute("DELETE FROM occurrences WHERE file = ?", (path,))
        cur.executemany(
            """INSERT OR REPLACE INTO symbols
               (id, name, qualname, kind, file, start_line, end_line, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (s.id, s.name, s.qualname, s.kind, s.file, s.start_line, s.end_line, s.signature)
                for s in symbols
            ],
        )
        cur.executemany(
            "INSERT INTO occurrences (file, src_symbol, name, type, line, module) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(o.file, o.src_symbol, o.name, o.type, o.line, o.module) for o in occurrences],
        )
        cur.execute(
            "INSERT OR REPLACE INTO files (path, hash, mtime, lang, indexed_at) VALUES (?, ?, ?, ?, ?)",
            (path, file_hash, mtime, lang, _utcnow()),
        )
        cur.commit()

    def remove_files(self, paths: Iterable[str]) -> None:
        paths = list(paths)
        if not paths:
            return
        marks = ",".join("?" * len(paths))
        self._conn.execute(f"DELETE FROM symbols WHERE file IN ({marks})", paths)
        self._conn.execute(f"DELETE FROM occurrences WHERE file IN ({marks})", paths)
        self._conn.execute(f"DELETE FROM files WHERE path IN ({marks})", paths)
        self._conn.commit()

    # --- edge (re)building ---------------------------------------------------
    def rebuild_edges(self) -> int:
        """Resolve every occurrence against the symbol table and rebuild edges.

        Resolution narrows a referenced name to its likeliest definition, in
        order of decreasing confidence:

          1. exactly one symbol has the name                  -> resolved
          2. exactly one of several shares the *caller's file* -> resolved
          3. several share the file                            -> heuristic (those)
          4. the caller imports the name ``from M`` and one
             candidate lives in a file matching ``M``          -> resolved
          5. several candidates match ``M``                    -> heuristic (those)
          6. nothing narrows it                                -> heuristic (all)

        A name with no internal match (stdlib / third-party) is dropped.
        Repeated occurrences (e.g. the same function called five times) collapse
        into a single edge whose ``count`` records how many occurrences produced
        it, instead of N duplicate rows.
        """
        name_to_ids: dict[str, list[str]] = defaultdict(list)
        id_to_file: dict[str, str] = {}
        for row in self._conn.execute(
            "SELECT id, name, file FROM symbols WHERE kind != 'module'"
        ):
            name_to_ids[row["name"]].append(row["id"])
            id_to_file[row["id"]] = row["file"]

        # file -> {imported_name -> source module}, used to disambiguate calls to
        # an imported name (`from M import f; f()`) towards the symbol in M.
        import_map: dict[str, dict[str, str]] = defaultdict(dict)
        for row in self._conn.execute(
            "SELECT file, name, module FROM occurrences WHERE type = 'import' AND module != ''"
        ):
            import_map[row["file"]].setdefault(row["name"], row["module"])

        def resolve(candidates: list[str], occ_file: str, name: str) -> tuple[list[str], str]:
            if len(candidates) == 1:
                return candidates, "resolved"
            same_file = [c for c in candidates if id_to_file.get(c) == occ_file]
            if len(same_file) == 1:
                return same_file, "resolved"
            if same_file:
                return same_file, "heuristic"
            module = import_map.get(occ_file, {}).get(name)
            if module:
                matched = [c for c in candidates if _module_matches(module, id_to_file.get(c, ""))]
                if len(matched) == 1:
                    return matched, "resolved"
                if matched:
                    return matched, "heuristic"
            return candidates, "heuristic"

        # (src, dst, name, type) -> occurrence count, with the chosen resolution.
        counts: dict[tuple, int] = defaultdict(int)
        resolutions: dict[tuple, str] = {}
        for row in self._conn.execute(
            "SELECT src_symbol, file, name, type FROM occurrences"
        ):
            candidates = name_to_ids.get(row["name"], [])
            if not candidates:
                continue
            chosen, resolution = resolve(candidates, row["file"], row["name"])
            for dst in chosen:
                if dst == row["src_symbol"]:
                    continue  # ignore trivial self-edges
                key = (row["src_symbol"], dst, row["name"], row["type"])
                counts[key] += 1
                resolutions[key] = resolution

        self._conn.execute("DELETE FROM edges")
        self._conn.executemany(
            "INSERT INTO edges (src_symbol, dst_symbol, dst_name, type, resolution, count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (src, dst, name, typ, resolutions[(src, dst, name, typ)], count)
                for (src, dst, name, typ), count in counts.items()
            ],
        )
        self._conn.commit()
        return len(counts)

    # --- queries -------------------------------------------------------------
    def find_symbol(self, name: str, kind: str | None = None) -> list[Symbol]:
        """Resolve a name (bare or qualified) to its definition(s)."""
        clauses = ["(name = ? OR qualname = ? OR id = ?)"]
        params: list = [name, name, name]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        rows = self._conn.execute(
            f"SELECT * FROM symbols WHERE {' AND '.join(clauses)} ORDER BY id", params
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        row = self._conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
        return _row_to_symbol(row) if row else None

    def get_symbols(self, symbol_ids: Iterable[str]) -> dict[str, Symbol]:
        """Batch-fetch symbols by id in one query, returned keyed by id.

        Avoids the N+1 round-trips of calling ``get_symbol`` in a loop. Missing
        ids are simply absent from the result. Callers stay capped at
        ``MAX_RESULTS`` ids, well under SQLite's bound-variable limit.
        """
        ids = list(symbol_ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT * FROM symbols WHERE id IN ({marks})", ids
        ).fetchall()
        return {row["id"]: _row_to_symbol(row) for row in rows}

    def symbol_files(self, symbol_ids: Iterable[str]) -> dict[str, str]:
        """Map symbol id -> defining file, batched in chunks under SQLite's
        bound-variable limit (so callers can pass an unbounded blast radius)."""
        ids = list(symbol_ids)
        out: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                f"SELECT id, file FROM symbols WHERE id IN ({marks})", chunk
            ):
                out[row["id"]] = row["file"]
        return out

    def file_outline(self, path: str) -> list[Symbol]:
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE file = ? AND kind != 'module' ORDER BY start_line", (path,)
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def callees(self, symbol_id: str) -> list[Edge]:
        return self._edges("src_symbol", symbol_id, types=("call",))

    def callers(self, symbol_id: str) -> list[Edge]:
        return self._edges("dst_symbol", symbol_id, types=("call",))

    def references(self, symbol_id: str) -> list[Edge]:
        return self._edges("dst_symbol", symbol_id, types=None)

    def _edges(self, column: str, symbol_id: str, types) -> list[Edge]:
        query = f"SELECT * FROM edges WHERE {column} = ?"
        params: list = [symbol_id]
        if types:
            marks = ",".join("?" * len(types))
            query += f" AND type IN ({marks})"
            params.extend(types)
        rows = self._conn.execute(query, params).fetchall()
        return [
            Edge(r["src_symbol"], r["dst_symbol"], r["dst_name"], r["type"],
                 r["resolution"], r["count"])
            for r in rows
        ]

    def blast_radius(
        self, symbol_id: str, max_depth: int = 2, resolved_only: bool = False
    ) -> list[tuple[str, int, str]]:
        """Transitive set of symbols that DEPEND ON ``symbol_id``.

        Walks edges in reverse (dependents): if A calls/references/inherits B,
        then A is in B's blast radius. Returns (symbol_id, depth, reached_via)
        excluding the seed; ``depth`` is the shortest path found and
        ``reached_via`` is "resolved" when at least one path to the symbol uses
        only resolved edges, else "heuristic" (its inclusion leans on a guessed
        edge). With ``resolved_only`` the walk follows resolved edges only.
        """
        edge_filter = "AND e.resolution = 'resolved'" if resolved_only else ""
        rows = self._conn.execute(
            f"""
            WITH RECURSIVE radius(symbol, depth, heur) AS (
                SELECT ?, 0, 0
                UNION
                SELECT e.src_symbol, r.depth + 1,
                       MAX(r.heur, CASE e.resolution WHEN 'resolved' THEN 0 ELSE 1 END)
                FROM edges e
                JOIN radius r ON e.dst_symbol = r.symbol
                WHERE r.depth < ? {edge_filter}
            )
            SELECT symbol, MIN(depth) AS depth, MIN(heur) AS heur
            FROM radius
            WHERE symbol != ?
            GROUP BY symbol
            ORDER BY depth, symbol
            """,
            (symbol_id, max_depth, symbol_id),
        ).fetchall()
        return [(r["symbol"], r["depth"], "heuristic" if r["heur"] else "resolved") for r in rows]

    def path_between(self, src_id: str, dst_id: str, max_depth: int = 6) -> list[str]:
        """Shortest directed dependency path from ``src_id`` to ``dst_id``.

        Walks edges *forward* (src calls/references/inherits dst), so the path
        reads as a call chain: ``src -> ... -> dst``. Returns the symbol ids in
        order (including both ends), or ``[]`` if no path within ``max_depth``.

        Cycles are pruned with a newline-delimited trail so a symbol id can't be
        mistaken for a substring of another (``a::foo`` vs ``a::foobar``).
        """
        if src_id == dst_id:
            return [src_id]
        row = self._conn.execute(
            """
            WITH RECURSIVE paths(symbol, depth, trail) AS (
                SELECT ?, 0, char(10) || ? || char(10)
                UNION ALL
                SELECT e.dst_symbol, p.depth + 1, p.trail || e.dst_symbol || char(10)
                FROM edges e
                JOIN paths p ON e.src_symbol = p.symbol
                WHERE p.depth < ?
                  AND instr(p.trail, char(10) || e.dst_symbol || char(10)) = 0
            )
            SELECT trail, depth FROM paths
            WHERE symbol = ?
            ORDER BY depth
            LIMIT 1
            """,
            (src_id, src_id, max_depth, dst_id),
        ).fetchone()
        if row is None:
            return []
        return [part for part in row["trail"].split("\n") if part]

    def symbols_in_files(self, paths: Iterable[str]) -> list[Symbol]:
        """All non-module symbols defined in the given repo-relative files."""
        paths = list(paths)
        if not paths:
            return []
        marks = ",".join("?" * len(paths))
        rows = self._conn.execute(
            f"SELECT * FROM symbols WHERE kind != 'module' AND file IN ({marks}) "
            "ORDER BY file, start_line",
            paths,
        ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def blast_radius_for_files(
        self, paths: Iterable[str], max_depth: int = 2, resolved_only: bool = False
    ) -> list[tuple[str, int, str]]:
        """Combined blast radius of every symbol defined in ``paths``.

        Seeds the reverse walk from all symbols in the changed files at once and
        returns dependents *outside* that set — i.e. what downstream code is
        affected by changing those files. (symbol_id, shortest_depth, reached_via)
        triples; see :meth:`blast_radius` for the ``reached_via`` semantics.
        """
        paths = list(paths)
        if not paths:
            return []
        marks = ",".join("?" * len(paths))
        edge_filter = "AND e.resolution = 'resolved'" if resolved_only else ""
        rows = self._conn.execute(
            f"""
            WITH RECURSIVE
            seeds(symbol) AS (
                SELECT id FROM symbols WHERE kind != 'module' AND file IN ({marks})
            ),
            radius(symbol, depth, heur) AS (
                SELECT symbol, 0, 0 FROM seeds
                UNION
                SELECT e.src_symbol, r.depth + 1,
                       MAX(r.heur, CASE e.resolution WHEN 'resolved' THEN 0 ELSE 1 END)
                FROM edges e
                JOIN radius r ON e.dst_symbol = r.symbol
                WHERE r.depth < ? {edge_filter}
            )
            SELECT symbol, MIN(depth) AS depth, MIN(heur) AS heur
            FROM radius
            WHERE symbol NOT IN (SELECT symbol FROM seeds)
            GROUP BY symbol
            ORDER BY depth, symbol
            """,
            (*paths, max_depth),
        ).fetchall()
        return [(r["symbol"], r["depth"], "heuristic" if r["heur"] else "resolved") for r in rows]

    def file_dependents(self) -> dict[str, int]:
        """Per-file cross-file in-degree: how many edges from *other* files point
        into a symbol defined in this file. A proxy for "how depended-upon".
        """
        rows = self._conn.execute(
            """
            SELECT dst.file AS file, COUNT(*) AS indeg
            FROM edges e
            JOIN symbols dst ON e.dst_symbol = dst.id
            JOIN symbols src ON e.src_symbol = src.id
            WHERE dst.file != src.file
            GROUP BY dst.file
            """
        ).fetchall()
        return {r["file"]: r["indeg"] for r in rows}

    def symbol_counts_by_file(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT file, COUNT(*) AS c FROM symbols WHERE kind != 'module' GROUP BY file"
        ).fetchall()
        return {r["file"]: r["c"] for r in rows}

    def resolution_stats(self) -> dict:
        """How many edges are confidently resolved vs heuristic, overall and per
        language (joined via the source symbol's file). A high heuristic rate for
        a language means its edges — and any blast radius through them — should be
        trusted less; surfaced so that confidence is measurable, not assumed.
        """
        overall = {"resolved": 0, "heuristic": 0}
        by_language: dict[str, dict[str, int]] = {}
        for row in self._conn.execute(
            """
            SELECT f.lang AS lang, e.resolution AS resolution, COUNT(*) AS c
            FROM edges e
            JOIN symbols s ON e.src_symbol = s.id
            JOIN files f ON s.file = f.path
            GROUP BY f.lang, e.resolution
            """
        ):
            res = row["resolution"]
            overall[res] = overall.get(res, 0) + row["c"]
            lang = by_language.setdefault(row["lang"], {"resolved": 0, "heuristic": 0})
            lang[res] = lang.get(res, 0) + row["c"]

        def with_rate(d: dict[str, int]) -> dict:
            total = d.get("resolved", 0) + d.get("heuristic", 0)
            rate = round(d.get("heuristic", 0) / total, 3) if total else 0.0
            return {**d, "total": total, "heuristic_rate": rate}

        return {
            "overall": with_rate(overall),
            "by_language": {lang: with_rate(d) for lang, d in sorted(by_language.items())},
        }

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

        return {
            "files": count("files"),
            "symbols": count("symbols"),
            "occurrences": count("occurrences"),
            "edges": count("edges"),
        }

    def close(self) -> None:
        self._conn.close()
