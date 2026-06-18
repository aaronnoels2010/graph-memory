"""Source -> (symbols, occurrences) using tree-sitter.

A single recursive walker handles every language; per-language node names come
from languages.py. The walker tracks a scope stack so each call/reference is
attributed to its enclosing symbol (or the synthetic ``<module>`` symbol for
top-level code).

This is *syntactic* analysis: it never imports or type-checks the target code.
Call resolution (occurrence name -> symbol id) happens later, in db.py, so it
can see the whole repo's symbol table.
"""
from __future__ import annotations

from .languages import get_parser, get_spec
from .models import Occurrence, ParseResult, Symbol

_MAX_SIG = 120


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _first_line(node) -> str:
    sig = _text(node).splitlines()[0].strip() if node.text else ""
    return sig[:_MAX_SIG]


# Node types that carry a bare name directly.
_NAME_NODES = ("identifier", "property_identifier", "name", "type_identifier", "field_identifier")
# Wrapper nodes (qualified/scoped/member access) whose trailing name we want.
_QUALIFIED_NODES = (
    "qualified_name", "scoped_type_identifier", "scoped_identifier",
    "generic_type", "generic_name",
)


def _trailing_name(node) -> str | None:
    """Return the rightmost bare identifier of a (possibly qualified) name node.

    Covers `foo`, `a.b.foo`, `A\\B\\foo` (php), member/attribute/property access,
    and generic types like `List<Foo>` -> the constructor/base `List`.
    """
    if node is None:
        return None
    if node.type in _NAME_NODES:
        return _text(node).split("\\")[-1].split(".")[-1]
    for field in ("name", "property", "attribute"):
        inner = node.child_by_field_name(field)
        if inner is not None:
            return _trailing_name(inner)
    idents = [c for c in node.children if c.type in _NAME_NODES]
    if idents:
        return _text(idents[-1]).split("\\")[-1].split(".")[-1]
    return None


def _callee_name(call_node) -> str | None:
    """Extract the bare callee name from any language's call node.

    Tries the fields that hold the callee across grammars, in order:
      - ``name``      java method_invocation, php member/scoped calls
      - ``function``  python call, js/ts call_expression, php function call, c# invocation
      - ``type``      object creation (constructor) where present
    Falls back to the first child. Returns None for fully dynamic calls.
    """
    for field in ("name", "function", "constructor", "type"):
        target = call_node.child_by_field_name(field)
        if target is not None:
            name = _trailing_name(target)
            if name:
                return name
    return _trailing_name(call_node.children[0]) if call_node.children else None


# Container nodes that hold base-class / interface lists across grammars.
_BASE_CONTAINERS = (
    "superclasses", "argument_list", "class_heritage",        # py / js / ts
    "superclass", "super_interfaces", "type_list",            # java
    "base_list",                                              # c#
    "base_clause", "class_interface_clause",                  # php
)


def _collect_type_names(node, names: list[str], depth: int = 0) -> None:
    if depth > 4:
        return
    for child in node.children:
        if child.type in _NAME_NODES:
            names.append(_text(child).split("\\")[-1].split(".")[-1])
        elif child.type in _QUALIFIED_NODES:
            trailing = _trailing_name(child)
            if trailing:
                names.append(trailing)
        elif child.type in _BASE_CONTAINERS:
            _collect_type_names(child, names, depth + 1)


def _base_names(class_node) -> list[str]:
    """Best-effort extraction of base-class / interface names."""
    names: list[str] = []
    field = class_node.child_by_field_name("superclasses")  # python
    if field is not None:
        _collect_type_names(field, names)
    for child in class_node.children:
        if child.type in _BASE_CONTAINERS:
            _collect_type_names(child, names)
    # de-duplicate while preserving order (python's field + child loop can overlap)
    return list(dict.fromkeys(names))


def _imported_names(import_node) -> list[str]:
    """Collect identifiers introduced by an import statement (best effort)."""
    names: list[str] = []

    def collect(node):
        if node.type in ("dotted_name", "identifier", "import_specifier", "name") + _QUALIFIED_NODES:
            ident = node.child_by_field_name("name") or node
            if ident.type in ("identifier", "dotted_name", "name") + _QUALIFIED_NODES:
                names.append(_text(ident).split("\\")[-1].split(".")[-1])
                return
        for child in node.children:
            collect(child)

    collect(import_node)
    return names


def parse_source(path: str, source: bytes, language: str) -> ParseResult:
    """Parse one file's bytes into definitions + raw reference occurrences."""
    spec = get_spec(language)
    parser = get_parser(language)
    tree = parser.parse(source)
    root = tree.root_node

    result = ParseResult()
    module_id = f"{path}::<module>"
    result.symbols.append(
        Symbol(
            id=module_id,
            name="<module>",
            qualname="<module>",
            kind="module",
            file=path,
            start_line=1,
            end_line=root.end_point[0] + 1,
            signature="",
        )
    )

    def add_def(node, scope_qual: str, in_class: bool) -> str:
        name_node = node.child_by_field_name(spec.name_field)
        name = _text(name_node) if name_node is not None else None
        if not name:
            return ""  # anonymous (e.g. default export) — skip, keep walking body
        qual = f"{scope_qual}.{name}" if scope_qual else name
        sid = f"{path}::{qual}"
        is_class = node.type in spec.class_nodes
        kind = "class" if is_class else ("method" if in_class else "function")
        result.symbols.append(
            Symbol(
                id=sid,
                name=name,
                qualname=qual,
                kind=kind,
                file=path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=_first_line(node),
            )
        )
        if is_class:
            for base in _base_names(node):
                result.occurrences.append(
                    Occurrence(path, sid, base, "base", node.start_point[0] + 1)
                )
        return sid

    def walk(node, scope_qual: str, enclosing_id: str, in_class: bool) -> None:
        for child in node.children:
            t = child.type
            if t in spec.function_nodes or t in spec.class_nodes:
                sid = add_def(child, scope_qual, in_class)
                if sid:
                    new_qual = sid.split("::", 1)[1]
                    walk(child, new_qual, sid, in_class=(t in spec.class_nodes))
                    continue
                # anonymous def: descend without changing scope
                walk(child, scope_qual, enclosing_id, in_class=False)
                continue

            if t in spec.call_nodes:
                callee = _callee_name(child)
                if callee:
                    result.occurrences.append(
                        Occurrence(path, enclosing_id, callee, "call", child.start_point[0] + 1)
                    )
            elif t in spec.import_nodes:
                for imported in _imported_names(child):
                    result.occurrences.append(
                        Occurrence(path, enclosing_id, imported, "import", child.start_point[0] + 1)
                    )

            walk(child, scope_qual, enclosing_id, in_class)

    walk(root, "", module_id, in_class=False)
    return result
