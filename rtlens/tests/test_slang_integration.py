"""Integration tests that require the slang_dump binary.

All tests in this module are unconditionally marked ``requires_slang`` and are
skipped unless:

1. The ``slang_dump`` binary exists at ``rtlens/bin/slang_dump`` (built from
   source), **and**
2. The environment variable ``RTLENS_SLANG_TEST=1`` is set.

Run these tests with::

    RTLENS_SLANG_TEST=1 python -m pytest rtlens/tests/test_slang_integration.py -v

"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import fixture_path

# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIN = _REPO_ROOT / "rtlens" / "bin" / "slang_dump"

_slang_available = _BIN.is_file() and os.environ.get("RTLENS_SLANG_TEST") == "1"

pytestmark = pytest.mark.requires_slang

skip_no_slang = pytest.mark.skipif(
    not _slang_available,
    reason="slang_dump binary not found or RTLENS_SLANG_TEST != 1",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(sv_file: str, top: str = ""):
    """Import here to avoid pulling in the slang backend at collection time."""
    from rtlens.slang_backend import load_design_with_slang

    return load_design_with_slang([fixture_path(sv_file)], top=top)


# ---------------------------------------------------------------------------
# simple_assign.sv
# ---------------------------------------------------------------------------

@skip_no_slang
class TestSlangSimpleAssign:
    def setup_method(self):
        self.db, self.cdb, self.log = _load("simple_assign.sv", top="simple_assign")

    def test_module_in_db(self):
        assert "simple_assign" in self.db.modules

    def test_hierarchy_root(self):
        assert len(self.db.roots) >= 1

    def test_log_contains_summary(self):
        assert "modules=" in self.log

    def test_cdb_has_signals(self):
        assert len(self.cdb.signal_to_source) > 0


# ---------------------------------------------------------------------------
# hierarchy.sv
# ---------------------------------------------------------------------------

@skip_no_slang
class TestSlangHierarchy:
    def setup_method(self):
        self.db, self.cdb, self.log = _load("hierarchy.sv", top="hierarchy_top")

    def test_top_module(self):
        assert self.db.top_module == "hierarchy_top"

    def test_hier_nodes_present(self):
        # At minimum the top + two logic_gate instances
        assert len(self.db.hier) >= 3

    def test_u0_in_hierarchy(self):
        matching = [p for p in self.db.hier if p.endswith(".u0")]
        assert len(matching) >= 1

    def test_u1_in_hierarchy(self):
        matching = [p for p in self.db.hier if p.endswith(".u1")]
        assert len(matching) >= 1


# ---------------------------------------------------------------------------
# counter.sv
# ---------------------------------------------------------------------------

@skip_no_slang
class TestSlangCounter:
    def setup_method(self):
        self.db, self.cdb, self.log = _load("counter.sv", top="counter")

    def test_module_found(self):
        assert "counter" in self.db.modules

    def test_cdb_has_clk_signal(self):
        clk_signals = [s for s in self.cdb.signal_to_source if s.endswith(".clk") or s == "clk"]
        assert len(clk_signals) >= 1


# ---------------------------------------------------------------------------
# Negative: non-existent file
# ---------------------------------------------------------------------------

@skip_no_slang
def test_slang_missing_file_raises():
    from rtlens.slang_backend import SlangBackendError

    with pytest.raises(SlangBackendError):
        _load("/nonexistent/path/no_such_file.sv")
