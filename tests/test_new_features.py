"""Tests for path_between, diff_blast_radius, and git-churn fragility."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# --- path_between -----------------------------------------------------------

def test_path_between_follows_call_chain(indexed):
    # main -> greet -> helper  (forward call chain)
    res = indexed.path_between("module_b.py::main", "module_a.py::helper")
    assert res["found"] is True
    ids = [step["id"] for step in res["path"]]
    assert ids[0] == "module_b.py::main"
    assert ids[-1] == "module_a.py::helper"
    assert "module_a.py::greet" in ids
    assert res["hops"] == len(ids) - 1


def test_path_between_is_directed(indexed):
    # helper never calls main, so there is no forward path back.
    res = indexed.path_between("module_a.py::helper", "module_b.py::main")
    assert res["found"] is False


def test_path_between_same_symbol_is_trivial(indexed):
    res = indexed.path_between("module_a.py::greet", "module_a.py::greet")
    assert res["found"] is True
    assert res["hops"] == 0


def test_path_between_missing_symbol_errors(indexed):
    assert "error" in indexed.path_between("nope", "module_a.py::helper")
    assert "error" in indexed.path_between("module_a.py::helper", "nope")


# --- diff_blast_radius (explicit files, no git needed) ----------------------

def test_diff_blast_radius_explicit_files(indexed):
    # Changing module_a.py affects module_b.main (which uses greet/Greeter).
    res = indexed.diff_blast_radius(files=["module_a.py"])
    assert res["changed_files"] == ["module_a.py"]
    assert "module_b.py" in res["by_file"]
    affected = {item["id"] for items in res["by_file"].values() for item in items}
    assert "module_b.py::main" in affected
    # Symbols defined in the changed file are seeds, not "affected".
    assert "module_a.py::greet" not in affected


def test_diff_blast_radius_unknown_file_is_reported(indexed):
    res = indexed.diff_blast_radius(files=["does/not/exist.py"])
    assert res["unknown_files"] == ["does/not/exist.py"]
    assert res["affected_count"] == 0


# --- git-backed: diff auto-detect + fragility -------------------------------

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark_git = pytest.mark.skipif(not _git_available(), reason="git not installed")


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytestmark_git
def test_diff_blast_radius_auto_detects_git_changes(indexed):
    root = indexed.settings.root_path
    _init_repo(root)
    # Touch module_a.py so `git diff HEAD` reports it.
    (root / "module_a.py").write_text(
        (root / "module_a.py").read_text() + "\n# edited\n"
    )
    res = indexed.diff_blast_radius()
    assert "module_a.py" in res["changed_files"]
    assert "module_b.py" in res["by_file"]


@pytestmark_git
def test_diff_blast_radius_non_git_root_errors(indexed):
    res = indexed.diff_blast_radius()  # sample repo is not a git work tree
    assert "error" in res and "git" in res["error"].lower()


@pytestmark_git
def test_fragility_ranks_churned_and_depended_on_files(indexed):
    root = indexed.settings.root_path
    _init_repo(root)
    # A second commit touching module_a.py raises its churn above module_b's.
    (root / "module_a.py").write_text(
        (root / "module_a.py").read_text() + "\n# churn\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "edit module_a")

    res = indexed.fragility()
    files = {row["file"]: row for row in res["fragile"]}
    # module_a is depended on (by module_b) AND churned -> appears with score>0.
    assert "module_a.py" in files
    assert files["module_a.py"]["churn_commits"] == 2
    assert files["module_a.py"]["dependents"] >= 1
    assert files["module_a.py"]["score"] > 0
    # module_b is not depended on by anything -> excluded (score 0).
    assert "module_b.py" not in files


@pytestmark_git
def test_fragility_non_git_root_errors(indexed):
    res = indexed.fragility()
    assert "error" in res
