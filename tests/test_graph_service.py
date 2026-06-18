"""End-to-end tests over the sample repo: definitions, edges, blast-radius."""
from __future__ import annotations


def test_find_symbol_resolves_definition(indexed):
    res = indexed.find_symbol("greet")
    assert res["count"] == 1
    sym = res["symbols"][0]
    assert sym["id"] == "module_a.py::greet"
    assert sym["kind"] == "function"
    assert sym["file"] == "module_a.py"
    assert "def greet" in sym["signature"]


def test_method_is_qualified_and_kind_method(indexed):
    res = indexed.find_symbol("Greeter.hello")
    assert res["count"] == 1
    assert res["symbols"][0]["kind"] == "method"
    assert res["symbols"][0]["id"] == "module_a.py::Greeter.hello"


def test_callers_of_greet(indexed):
    callers = {c["id"] for c in indexed.callers("module_a.py::greet")["callers"]}
    # called by Greeter.hello (module_a) and main (module_b)
    assert "module_a.py::Greeter.hello" in callers
    assert "module_b.py::main" in callers


def test_callees_of_hello_includes_greet(indexed):
    callees = {c["id"] for c in indexed.callees("module_a.py::Greeter.hello")["callees"]}
    assert "module_a.py::greet" in callees


def test_callees_of_greet_includes_helper(indexed):
    callees = {c["id"] for c in indexed.callees("module_a.py::greet")["callees"]}
    assert "module_a.py::helper" in callees


def test_blast_radius_of_helper(indexed):
    res = indexed.blast_radius("module_a.py::helper", max_depth=3)
    affected = {
        item["id"]
        for items in res["by_file"].values()
        for item in items
    }
    # helper <- greet <- {Greeter.hello, main}
    assert "module_a.py::greet" in affected
    assert "module_a.py::Greeter.hello" in affected
    assert "module_b.py::main" in affected


def test_inheritance_edge(indexed):
    # Greeter has no base here, but the class itself must be found
    res = indexed.find_symbol("Greeter")
    assert res["count"] == 1
    assert res["symbols"][0]["kind"] == "class"


def test_file_outline(indexed):
    outline = indexed.file_outline("module_a.py")
    kinds = {s["qualname"]: s["kind"] for s in outline["symbols"]}
    assert kinds["greet"] == "function"
    assert kinds["Greeter"] == "class"
    assert kinds["Greeter.hello"] == "method"


def test_typescript_call_graph(indexed):
    callers = {c["id"] for c in indexed.callers("sample.ts::add")["callers"]}
    assert "sample.ts::run" in callers


def test_ambiguous_reference_returns_candidates(service):
    # define `dup` in two files, then a bare-name edge query is ambiguous
    root = service.settings.root_path
    (root / "dup_a.py").write_text("def dup():\n    return 1\n")
    (root / "dup_b.py").write_text("def dup():\n    return 2\n")
    service.index()
    res = service.callers("dup")
    assert "error" in res and "ambiguous" in res["error"]
    assert len(res["candidates"]) == 2


def test_missing_symbol_returns_error(indexed):
    res = indexed.blast_radius("does_not_exist")
    assert "error" in res


def test_repeated_calls_collapse_into_one_edge_with_count(service):
    # `caller` invokes `target` three times: expect ONE edge carrying count == 3,
    # not three duplicate edges inflating the result.
    root = service.settings.root_path
    (root / "repeat.py").write_text(
        "def target():\n"
        "    return 1\n\n\n"
        "def caller():\n"
        "    target()\n"
        "    target()\n"
        "    target()\n"
        "    return 0\n"
    )
    service.index()

    res = service.callees("repeat.py::caller")
    targets = [c for c in res["callees"] if c["id"] == "repeat.py::target"]
    assert len(targets) == 1
    assert targets[0]["count"] == 3
    assert res["count"] == 1  # one distinct edge, not three

    # ...and symmetrically from the callee's side.
    callers = service.callers("repeat.py::target")["callers"]
    caller_edge = [c for c in callers if c["id"] == "repeat.py::caller"]
    assert len(caller_edge) == 1
    assert caller_edge[0]["count"] == 3


def test_get_symbols_batches_and_skips_missing(indexed):
    found = indexed.db.get_symbols(
        ["module_a.py::greet", "module_a.py::Greeter.hello", "does_not_exist"]
    )
    assert set(found) == {"module_a.py::greet", "module_a.py::Greeter.hello"}
    assert found["module_a.py::greet"].name == "greet"
    assert indexed.db.get_symbols([]) == {}
