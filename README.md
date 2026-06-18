# Codebase Knowledge Graph

A **local-first codebase knowledge graph for AI agents**. It indexes a repo into
a queryable graph of symbols (modules, classes, functions, methods) and their
relationships (calls, references, inheritance), so an agent can answer
*structural* questions in a single tool call instead of re-reading files and
re-deriving structure every session.

- **Tree-sitter** parsing — purely syntactic; it **never imports or runs** the target code
- **SQLite** graph store (symbols + occurrences + derived edges), source of truth on local disk
- **Incremental** indexing — only changed files are re-parsed (content-hash based)
- **Honest resolution** — call edges are flagged `resolved` (unique match) or `heuristic` (ambiguous)
- **Token-efficient output** — every query returns compact, `file:line`-anchored dicts, size-capped
- **In-process MCP server** — no HTTP hop; one venv
- **Pluggable languages** — Python, TypeScript, JavaScript, Java, C#, and PHP out of the box; add one `LanguageSpec` to extend
- **Tested** — pytest suite over a multi-language sample repo + incremental-reindex proofs

> Sibling project to `persistent-memory`. They're intentionally separate
> servers (different data models, query paradigms, and lifecycles); run both and
> register both with your MCP client. A natural future integration: this graph
> can *emit* facts into the memory service ("module X is fragile, changed often")
> over its public API — a clean one-directional dependency.

---

## Layout

```
app/
  config.py         settings (env / .env)
  logging_config.py stderr logging (stdout is reserved for MCP stdio)
  exceptions.py     domain exceptions
  models.py         Symbol / Occurrence / Edge dataclasses
  languages.py      pluggable tree-sitter registry
  parser.py         source -> (symbols, occurrences) via a single recursive walker
  db.py             SQLite store + graph queries (recursive CTE for blast radius)
  indexer.py        repo walk + incremental (hash-based) reindex
  graph_service.py  high-level, token-efficient query API
mcp_server/
  server.py         FastMCP stdio server (imports app.graph_service directly)
tests/              sample-repo + incremental tests
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Register with Claude Code (user scope — works from any repo)

Register once at user scope, with **no** `ROOT_PATH`, so it indexes whichever
repo you launch Claude Code in:

```bash
claude mcp add codebase-graph --scope user -- \
  /path/to/graph-memory/.venv/bin/python /path/to/graph-memory/mcp_server/server.py
```

Each indexed repo gets its own cached DB under `~/.graph-memory/` (keyed by repo
path), so graphs never collide and reindexing stays incremental across sessions.

> **How the "any repo" default works:** a user-scoped stdio server is launched
> with its working directory set to the repo you opened Claude Code in, and
> `ROOT_PATH` defaults to `.`. So `index_codebase()` with no argument indexes the
> current repo. You can always pass an explicit absolute `path` to index elsewhere.

### Pinning to one fixed repo instead

If you'd rather a server always index one specific project, set `ROOT_PATH`:

```bash
claude mcp add codebase-graph --scope user \
  --env ROOT_PATH=/path/to/your/repo -- \
  /path/to/graph-memory/.venv/bin/python /path/to/graph-memory/mcp_server/server.py
```

### Run standalone (debugging)

```bash
ROOT_PATH=/path/to/your/repo python mcp_server/server.py
```

## Tools

| Tool | Answers | Example |
|------|---------|---------|
| `index_codebase(path?, languages?, force_full?)` | build/refresh the index | `index_codebase()` |
| `find_symbol(name, kind?)` | where is X defined? | `find_symbol("greet")` |
| `find_references(symbol, limit?)` | everywhere X is used | `find_references("Greeter.hello")` |
| `get_callers(symbol, limit?)` | who calls X? | `get_callers("module_a.py::greet")` |
| `get_callees(symbol, limit?)` | what does X call? | `get_callees("main")` |
| `blast_radius(symbol, max_depth?)` | what breaks if X changes? | `blast_radius("helper", 3)` |
| `get_file_outline(path)` | what's in this file? | `get_file_outline("app/db.py")` |
| `path_between(src, dst, max_depth?)` | how does X reach Y? | `path_between("main", "helper")` |
| `diff_blast_radius(files?, max_depth?)` | what does my PR affect? | `diff_blast_radius()` |
| `fragility(limit?, max_commits?)` | which files are riskiest to touch? | `fragility(20)` |

Names can be bare (`greet`), qualified (`Greeter.hello`), or a full id
(`module_a.py::greet`). When a bare name is ambiguous, queries return the
candidate list so you can re-ask with a full id.

## Supported languages

| Language | Extensions | Notes |
|---|---|---|
| Python | `.py` `.pyi` | highest precision |
| TypeScript | `.ts` `.tsx` | |
| JavaScript | `.js` `.jsx` `.mjs` `.cjs` | |
| Java | `.java` | classes/interfaces/enums, `extends`/`implements` edges |
| C# | `.cs` | overloads resolve by name → more `heuristic` edges |
| PHP | `.php` | function, member (`->`), and static (`::`) call forms |

Overload-heavy languages (Java, C#) and PHP's large stdlib mean more edges are
flagged `heuristic` and more external calls are dropped — expect lower precision
than Python. To restrict indexing to a subset, pass `languages=[...]` to
`index_codebase` or set the `LANGUAGES` env var.

## Typical agent flow

1. `index_codebase()` once at the start of a session.
2. `blast_radius("the_thing_im_about_to_change")` before editing — get the
   affected set grouped by file.
3. `get_callers` / `get_callees` to navigate the call graph one hop at a time.
4. `path_between(a, b)` to see *how* one symbol reaches another (the call chain),
   not just *that* it does.
5. `diff_blast_radius()` mid-PR — auto-detects your `git diff` (staged, unstaged,
   untracked) and returns the combined downstream impact, grouped by file.
6. `fragility()` to triage: files that churn often **and** are widely
   depended-upon are the riskiest to change (needs a git work tree).

## Limitations (by design — static analysis)

- **Dynamic dispatch / duck typing**: a call through a variable whose type isn't
  syntactically known resolves by *name*. Multiple definitions of that name →
  `heuristic` edges to all candidates; truly dynamic calls may be missed.
- **No cross-name resolution**: import aliasing (`import x as y`) and attribute
  chains beyond the trailing name aren't followed.
- **External calls dropped**: references that don't resolve to an indexed symbol
  (stdlib, third-party) are not stored as edges.
- **.gitignore is approximated**: common build/vendor dirs and dot-dirs are
  skipped; arbitrary `.gitignore` rules are not parsed.

These are deliberate trade-offs for speed and zero-execution safety. Treat edges
as strong hints, not proof.

## Tests

```bash
pytest
```
