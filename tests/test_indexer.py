"""Incremental-reindex behaviour: only changed files are re-parsed."""
from __future__ import annotations


def test_full_index_then_noop_reindex(service):
    first = service.index()
    assert first["parsed_files"] == 3  # module_a, module_b, sample.ts
    assert first["symbols"] > 0

    # Nothing changed -> nothing re-parsed.
    second = service.index()
    assert second["parsed_files"] == 0
    assert second["skipped_files"] == 3


def test_only_changed_file_is_reparsed(service):
    service.index()
    root = service.settings.root_path
    (root / "module_b.py").write_text(
        "from module_a import greet\n\n\ndef main():\n    return greet('z')\n"
    )

    result = service.index()
    assert result["parsed_files"] == 1
    assert result["skipped_files"] == 2


def test_force_full_reparses_everything(service):
    service.index()
    result = service.index(force_full=True)
    assert result["parsed_files"] == 3
    assert result["skipped_files"] == 0


def test_deleted_file_is_removed_from_graph(service):
    service.index()
    root = service.settings.root_path
    (root / "module_b.py").unlink()

    result = service.index()
    assert result["removed_files"] == 1
    # main lived in module_b; it should be gone now.
    assert service.find_symbol("main")["count"] == 0


def test_new_file_edges_resolve_after_incremental(service):
    service.index()
    root = service.settings.root_path
    (root / "module_c.py").write_text(
        "from module_a import greet\n\n\ndef extra():\n    return greet('c')\n"
    )
    service.index()
    callers = {c["id"] for c in service.callers("module_a.py::greet")["callers"]}
    assert "module_c.py::extra" in callers
