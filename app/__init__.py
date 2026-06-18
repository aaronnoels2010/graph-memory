"""Codebase Knowledge Graph — index a repo into a queryable graph of symbols.

The package is organised as:
  - config.py        application settings (env / .env)
  - logging_config.py central logging setup
  - exceptions.py     domain exceptions
  - models.py         plain dataclasses for symbols / edges / parse results
  - languages.py      pluggable tree-sitter language registry
  - parser.py         source -> (symbols, occurrences) via tree-sitter
  - db.py             SQLite store + graph queries (recursive CTEs)
  - indexer.py        repo walk + incremental (hash-based) (re)indexing
  - graph_service.py  high-level, token-efficient query API used by the MCP server
"""
