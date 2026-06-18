"""Java / C# / PHP support: definitions, call graph, and inheritance edges."""
from __future__ import annotations


def test_polyglot_index_counts(poly_indexed):
    stats = poly_indexed.stats()
    assert stats["files"] == 3  # Sample.java, Sample.cs, sample.php
    assert stats["symbols"] > 0
    assert stats["edges"] > 0


# --- Java -------------------------------------------------------------------
def test_java_method_definition(poly_indexed):
    res = poly_indexed.find_symbol("jgreet")
    assert res["count"] == 1
    sym = res["symbols"][0]
    assert sym["id"] == "Sample.java::JUtil.jgreet"
    assert sym["kind"] == "method"
    assert sym["file"] == "Sample.java"


def test_java_call_graph(poly_indexed):
    callers = {c["id"] for c in poly_indexed.callers("Sample.java::JUtil.jgreet")["callers"]}
    assert "Sample.java::JGreeter.jhello" in callers
    callees = {c["id"] for c in poly_indexed.callees("Sample.java::JUtil.jgreet")["callees"]}
    assert "Sample.java::JUtil.jhelper" in callees


def test_java_inheritance_edge(poly_indexed):
    refs = poly_indexed.references("Sample.java::JUtil")["references"]
    assert any(r["id"] == "Sample.java::JGreeter" and r["type"] == "base" for r in refs)


def test_java_blast_radius(poly_indexed):
    res = poly_indexed.blast_radius("Sample.java::JUtil.jhelper", max_depth=3)
    affected = {i["id"] for items in res["by_file"].values() for i in items}
    assert "Sample.java::JUtil.jgreet" in affected
    assert "Sample.java::JGreeter.jhello" in affected


# --- C# ---------------------------------------------------------------------
def test_csharp_method_definition(poly_indexed):
    res = poly_indexed.find_symbol("cgreet")
    assert res["count"] == 1
    assert res["symbols"][0]["id"] == "Sample.cs::CUtil.cgreet"
    assert res["symbols"][0]["kind"] == "method"


def test_csharp_call_graph(poly_indexed):
    callers = {c["id"] for c in poly_indexed.callers("Sample.cs::CUtil.cgreet")["callers"]}
    assert "Sample.cs::CGreeter.chello" in callers


def test_csharp_inheritance_edge(poly_indexed):
    refs = poly_indexed.references("Sample.cs::CUtil")["references"]
    assert any(r["id"] == "Sample.cs::CGreeter" and r["type"] == "base" for r in refs)


# --- PHP --------------------------------------------------------------------
def test_php_function_definition(poly_indexed):
    res = poly_indexed.find_symbol("pgreet")
    assert res["count"] == 1
    assert res["symbols"][0]["id"] == "sample.php::pgreet"
    assert res["symbols"][0]["kind"] == "function"


def test_php_method_is_qualified(poly_indexed):
    res = poly_indexed.find_symbol("phello")
    assert res["count"] == 1
    assert res["symbols"][0]["id"] == "sample.php::PGreeter.phello"
    assert res["symbols"][0]["kind"] == "method"


def test_php_call_graph(poly_indexed):
    callers = {c["id"] for c in poly_indexed.callers("sample.php::pgreet")["callers"]}
    assert "sample.php::PGreeter.phello" in callers
    callees = {c["id"] for c in poly_indexed.callees("sample.php::pgreet")["callees"]}
    assert "sample.php::phelper" in callees
