"""Shared fixtures: a tiny multi-language sample repo built in a tmp dir.

Building the sample in tmp_path (rather than committing fixture files) keeps the
incremental-reindex test honest — it can freely mutate files and re-index.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.graph_service import GraphService

PY_MODULE_A = '''\
def helper():
    return 1


def greet(name):
    helper()
    return f"hi {name}"


class Greeter:
    def __init__(self):
        self.n = 0

    def hello(self):
        return greet("world")
'''

PY_MODULE_B = '''\
from module_a import greet, Greeter


def main():
    greet("y")
    g = Greeter()
    return g.hello()
'''

TS_SAMPLE = '''\
function add(a: number, b: number): number {
  return a + b;
}

function run(): number {
  return add(1, 2);
}
'''


def _write_sample(root: Path) -> None:
    (root / "module_a.py").write_text(PY_MODULE_A)
    (root / "module_b.py").write_text(PY_MODULE_B)
    (root / "sample.ts").write_text(TS_SAMPLE)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _write_sample(root)
    return root


@pytest.fixture
def service(tmp_path: Path, sample_repo: Path) -> GraphService:
    settings = Settings(data_dir=tmp_path / "data", root_path=sample_repo)
    svc = GraphService(settings=settings)
    yield svc
    svc.close()


@pytest.fixture
def indexed(service: GraphService) -> GraphService:
    service.index()
    return service


# --- polyglot fixtures (java / c# / php) ------------------------------------
# Kept in a separate repo from the py/ts sample so symbol names don't collide
# and the python/ts file-count assertions stay stable. Names are unique across
# the three files for the same reason.

JAVA_SAMPLE = """\
class JUtil {
    int jhelper() { return 1; }
    int jgreet() { return jhelper(); }
}

class JGreeter extends JUtil {
    int jhello() { return jgreet(); }
}
"""

CSHARP_SAMPLE = """\
class CUtil {
    int chelper() { return 1; }
    int cgreet() { return chelper(); }
}

class CGreeter : CUtil {
    int chello() { return cgreet(); }
}
"""

PHP_SAMPLE = """\
<?php
function phelper() { return 1; }

function pgreet() { return phelper(); }

class PGreeter {
    public function phello() {
        return pgreet();
    }
}
"""


@pytest.fixture
def polyglot_repo(tmp_path: Path) -> Path:
    root = tmp_path / "poly"
    root.mkdir()
    (root / "Sample.java").write_text(JAVA_SAMPLE)
    (root / "Sample.cs").write_text(CSHARP_SAMPLE)
    (root / "sample.php").write_text(PHP_SAMPLE)
    return root


@pytest.fixture
def poly_indexed(tmp_path: Path, polyglot_repo: Path) -> GraphService:
    settings = Settings(data_dir=tmp_path / "data-poly", root_path=polyglot_repo)
    svc = GraphService(settings=settings)
    svc.index()
    yield svc
    svc.close()
