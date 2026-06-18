"""Pluggable tree-sitter language registry.

Adding a language = adding one LanguageSpec entry plus the matching
`tree-sitter-<lang>` dependency. The spec is intentionally small: it names the
node types the walker (parser.py) cares about, so the walking logic stays
language-agnostic.

Parsers/languages are built lazily and cached, because importing the grammar
modules and constructing a Language is comparatively expensive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from tree_sitter import Language, Parser

from .exceptions import UnsupportedLanguageError


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    # node types that introduce a definition
    function_nodes: frozenset[str]
    class_nodes: frozenset[str]
    # node types that are call sites
    call_nodes: frozenset[str]
    # node types that are import statements
    import_nodes: frozenset[str]
    # how to obtain the underlying tree_sitter.Language
    _loader: str = ""           # dotted "module:attr" producing the raw language
    name_field: str = "name"    # field holding a def's identifier


# module:attr pairs are resolved lazily so the package imports even if an
# individual grammar isn't installed.
SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        function_nodes=frozenset({"function_definition"}),
        class_nodes=frozenset({"class_definition"}),
        call_nodes=frozenset({"call"}),
        import_nodes=frozenset({"import_statement", "import_from_statement"}),
        _loader="tree_sitter_python:language",
    ),
    "javascript": LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        function_nodes=frozenset(
            {"function_declaration", "method_definition", "generator_function_declaration"}
        ),
        class_nodes=frozenset({"class_declaration"}),
        call_nodes=frozenset({"call_expression"}),
        import_nodes=frozenset({"import_statement"}),
        _loader="tree_sitter_javascript:language",
    ),
    "typescript": LanguageSpec(
        name="typescript",
        extensions=(".ts", ".tsx"),
        function_nodes=frozenset(
            {
                "function_declaration",
                "method_definition",
                "generator_function_declaration",
                "abstract_method_signature",
            }
        ),
        class_nodes=frozenset({"class_declaration", "abstract_class_declaration"}),
        call_nodes=frozenset({"call_expression"}),
        import_nodes=frozenset({"import_statement"}),
        # typescript grammar exposes language_typescript() (and language_tsx()).
        _loader="tree_sitter_typescript:language_typescript",
    ),
    "java": LanguageSpec(
        name="java",
        extensions=(".java",),
        function_nodes=frozenset({"method_declaration", "constructor_declaration"}),
        class_nodes=frozenset(
            {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
        ),
        call_nodes=frozenset({"method_invocation"}),
        import_nodes=frozenset({"import_declaration"}),
        _loader="tree_sitter_java:language",
    ),
    "csharp": LanguageSpec(
        name="csharp",
        extensions=(".cs",),
        function_nodes=frozenset(
            {"method_declaration", "constructor_declaration", "local_function_statement"}
        ),
        class_nodes=frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "struct_declaration",
                "record_declaration",
                "enum_declaration",
            }
        ),
        call_nodes=frozenset({"invocation_expression"}),
        import_nodes=frozenset({"using_directive"}),
        _loader="tree_sitter_c_sharp:language",
    ),
    "php": LanguageSpec(
        name="php",
        extensions=(".php",),
        function_nodes=frozenset({"function_definition", "method_declaration"}),
        class_nodes=frozenset(
            {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}
        ),
        call_nodes=frozenset(
            {"function_call_expression", "member_call_expression", "scoped_call_expression"}
        ),
        import_nodes=frozenset({"namespace_use_declaration"}),
        # the php grammar exposes language_php() (with HTML) and language_php_only().
        _loader="tree_sitter_php:language_php",
    ),
}

# Built once from SPECS at import time.
_EXT_TO_LANG: dict[str, str] = {
    ext: spec.name for spec in SPECS.values() for ext in spec.extensions
}


def get_spec(language: str) -> LanguageSpec:
    try:
        return SPECS[language]
    except KeyError as exc:
        raise UnsupportedLanguageError(language) from exc


def language_for_path(path: str) -> str | None:
    """Map a file path to a registered language name (or None to skip)."""
    for ext, lang in _EXT_TO_LANG.items():
        if path.endswith(ext):
            return lang
    return None


def _load_raw_language(loader: str):
    module_name, _, attr = loader.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)()


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser:
    """Return a cached tree-sitter Parser for the language.

    Defensive across tree-sitter binding versions: 0.22+ accepts
    ``Parser(Language(...))``; older releases want ``parser.language = ...``.
    """
    spec = get_spec(language)
    if not spec._loader:
        raise UnsupportedLanguageError(language)

    raw = _load_raw_language(spec._loader)
    ts_language = Language(raw)
    try:
        return Parser(ts_language)
    except TypeError:
        parser = Parser()
        parser.language = ts_language  # type: ignore[attr-defined]
        return parser
