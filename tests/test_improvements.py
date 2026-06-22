"""Tests for the precision/confidence/test-mapping/staleness improvements.

Each builds its own tiny repo in tmp_path so name collisions (needed to prove
disambiguation) stay isolated from the shared sample fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import indexer
from app.config import Settings
from app.graph_service import GraphService, _is_test_file


def _service(tmp_path: Path, name: str, files: dict[str, str]) -> GraphService:
    root = tmp_path / name
    root.mkdir()
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    svc = GraphService(settings=Settings(data_dir=tmp_path / f"data-{name}", root_path=root))
    svc.index()
    return svc


# --- same-file resolution ----------------------------------------------------

def test_same_file_call_resolves_locally(tmp_path):
    svc = _service(tmp_path, "samefile", {
        "a.py": "def helper():\n    return 1\n\n\ndef use_a():\n    return helper()\n",
        "b.py": "def helper():\n    return 2\n\n\ndef use_b():\n    return helper()\n",
    })
    try:
        # `helper` is defined in both files, but a.use_a's call must resolve to
        # a.helper only — not fan out to b.helper as a heuristic edge.
        callers = svc.callers("a.py::helper")["callers"]
        ids = {c["id"]: c for c in callers}
        assert "a.py::use_a" in ids
        assert "b.py::use_b" not in ids
        assert ids["a.py::use_a"]["resolution"] == "resolved"
    finally:
        svc.close()


# --- import-aware resolution -------------------------------------------------

def test_import_disambiguates_across_files(tmp_path):
    svc = _service(tmp_path, "imports", {
        "mod_a.py": "def shared():\n    return 1\n",
        "mod_c.py": "def shared():\n    return 2\n",
        "consumer.py": (
            "from mod_a import shared\n\n\n"
            "def run():\n    return shared()\n"
        ),
    })
    try:
        # consumer imports `shared` from mod_a, so run()'s call resolves there.
        a_callers = {c["id"]: c for c in svc.callers("mod_a.py::shared")["callers"]}
        c_callers = {c["id"] for c in svc.callers("mod_c.py::shared")["callers"]}
        assert "consumer.py::run" in a_callers
        assert a_callers["consumer.py::run"]["resolution"] == "resolved"
        assert "consumer.py::run" not in c_callers
    finally:
        svc.close()


# --- confidence propagation + resolved_only ----------------------------------

def test_blast_radius_flags_heuristic_and_resolved_only_filters(tmp_path):
    svc = _service(tmp_path, "confidence", {
        "core.py": "def widget():\n    return 1\n",
        "other.py": "def widget():\n    return 2\n",
        "mid.py": "def caller():\n    return widget()\n",
    })
    try:
        # `widget` is ambiguous and mid.caller neither shares a file nor imports
        # it -> heuristic edges to both. So caller is reached via a guess.
        res = svc.blast_radius("core.py::widget", max_depth=2)
        affected = {
            item["id"]: item
            for items in res["by_file"].values() for item in items
        }
        assert "mid.py::caller" in affected
        assert affected["mid.py::caller"]["reached_via"] == "heuristic"
        assert res["heuristic_count"] >= 1

        # resolved_only drops the guessed dependent entirely.
        strict = svc.blast_radius("core.py::widget", max_depth=2, resolved_only=True)
        strict_ids = {
            item["id"] for items in strict["by_file"].values() for item in items
        }
        assert "mid.py::caller" not in strict_ids
    finally:
        svc.close()


def test_blast_radius_resolved_path_is_marked_resolved(tmp_path):
    svc = _service(tmp_path, "resolved", {
        "a.py": "def helper():\n    return 1\n\n\ndef use_a():\n    return helper()\n",
        "b.py": "def helper():\n    return 2\n",  # makes the name ambiguous globally
    })
    try:
        res = svc.blast_radius("a.py::helper", max_depth=2)
        affected = {
            item["id"]: item
            for items in res["by_file"].values() for item in items
        }
        assert affected["a.py::use_a"]["reached_via"] == "resolved"
        assert res["heuristic_count"] == 0
    finally:
        svc.close()


# --- affected_tests ----------------------------------------------------------

def test_affected_tests_finds_covering_tests(tmp_path):
    svc = _service(tmp_path, "tests", {
        "src.py": "def feature():\n    return 1\n",
        "tests/test_feature.py": (
            "from src import feature\n\n\n"
            "def test_feature():\n    assert feature() == 1\n"
        ),
        "util.py": "def feature_user():\n    from src import feature\n    return feature()\n",
    })
    try:
        res = svc.affected_tests("feature")
        assert res["test_count"] >= 1
        assert "tests/test_feature.py" in res["test_files"]
        ids = {item["id"] for items in res["by_file"].values() for item in items}
        assert "tests/test_feature.py::test_feature" in ids
        # non-test dependents must not be reported here
        assert "util.py::feature_user" not in ids
    finally:
        svc.close()


@pytest.mark.parametrize("path,expected", [
    ("tests/test_x.py", True),
    ("src/foo_test.py", True),
    ("pkg/__tests__/a.js", True),
    ("app/Widget.spec.ts", True),
    ("com/example/WidgetTest.java", True),
    ("WidgetTests.cs", True),
    ("src/feature.py", False),
    ("src/fastest.py", False),
    ("lib/contest.py", False),
])
def test_is_test_file(path, expected):
    assert _is_test_file(path) is expected


# --- staleness ---------------------------------------------------------------

def test_status_tracks_changed_added_removed(tmp_path):
    svc = _service(tmp_path, "stale", {
        "a.py": "def f():\n    return 1\n",
        "b.py": "def g():\n    return 2\n",
    })
    try:
        root = svc.settings.root_path
        fresh = svc.status()
        assert fresh["indexed"] is True
        assert fresh["stale"] is False

        # modify a.py and push its mtime forward so the cheap mtime check trips
        (root / "a.py").write_text("def f():\n    return 99\n")
        future = os.stat(root / "a.py").st_mtime + 100
        os.utime(root / "a.py", (future, future))
        # add a new file, remove an existing one
        (root / "c.py").write_text("def h():\n    return 3\n")
        (root / "b.py").unlink()

        st = svc.status()
        assert st["stale"] is True
        assert st["changed_files"] == ["a.py"]
        assert st["added_files"] == ["c.py"]
        assert st["removed_files"] == ["b.py"]
    finally:
        svc.close()


# --- resolution stats --------------------------------------------------------

def test_index_summary_and_stats_report_resolution(tmp_path):
    svc = _service(tmp_path, "stats", {
        "m.py": "def a():\n    return 1\n\n\ndef b():\n    return a()\n",
    })
    try:
        summary = svc.index()  # re-index to capture the returned summary
        assert "resolution" in summary
        overall = summary["resolution"]["overall"]
        assert overall["total"] >= 1
        assert 0.0 <= overall["heuristic_rate"] <= 1.0
        assert "python" in summary["resolution"]["by_language"]
        assert "resolution" in svc.stats()
    finally:
        svc.close()


# --- gitignore (only when pathspec is installed) -----------------------------

@pytest.mark.skipif(indexer.pathspec is None, reason="pathspec not installed")
def test_gitignore_excludes_matched_files(tmp_path):
    svc = _service(tmp_path, "gi", {
        ".gitignore": "ignored.py\ngenerated/\n",
        "ignored.py": "def secret():\n    return 1\n",
        "generated/gen.py": "def gen():\n    return 1\n",
        "kept.py": "def kept():\n    return 1\n",
    })
    try:
        assert svc.find_symbol("kept")["count"] == 1
        assert svc.find_symbol("secret")["count"] == 0
        assert svc.find_symbol("gen")["count"] == 0
    finally:
        svc.close()
