"""Tests for sv_parser.parse_sv_files() basic behaviour."""
from __future__ import annotations

import pytest

from rtlens.sv_parser import parse_sv_files
from rtlens.model import DesignDB

from .conftest import fixture_path


# ---------------------------------------------------------------------------
# simple_assign.sv
# ---------------------------------------------------------------------------

class TestParseSimpleAssign:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("simple_assign.sv")])
        self.mod = self.db.modules["simple_assign"]

    def test_module_found(self):
        assert "simple_assign" in self.db.modules

    def test_port_names(self):
        assert set(self.mod.ports.keys()) == {"a", "b", "sel", "y", "eq"}

    def test_input_directions(self):
        for name in ("a", "b", "sel"):
            assert self.mod.ports[name].direction == "input"

    def test_output_directions(self):
        for name in ("y", "eq"):
            assert self.mod.ports[name].direction == "output"

    def test_assign_count(self):
        assert len(self.mod.assignments) == 2

    def test_first_assign_lhs_is_y(self):
        lhses = [a.lhs for a in self.mod.assignments]
        assert ["y"] in lhses

    def test_no_instances(self):
        assert len(self.mod.instances) == 0

    def test_no_always_blocks(self):
        assert len(self.mod.always_blocks) == 0


# ---------------------------------------------------------------------------
# counter.sv
# ---------------------------------------------------------------------------

class TestParseCounter:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("counter.sv")])
        self.mod = self.db.modules["counter"]

    def test_module_found(self):
        assert "counter" in self.db.modules

    def test_ports(self):
        assert set(self.mod.ports.keys()) == {"clk", "rst_n", "en", "q"}

    def test_internal_signal_cnt(self):
        assert "cnt" in self.mod.signals
        assert self.mod.signals["cnt"].kind == "logic"

    def test_always_ff_block(self):
        ff = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff) == 1

    def test_always_ff_clock_signal(self):
        ff = next(b for b in self.mod.always_blocks if b.kind == "always_ff")
        assert "clk" in ff.clock_signals

    def test_always_ff_reset_signal(self):
        ff = next(b for b in self.mod.always_blocks if b.kind == "always_ff")
        assert "rst_n" in ff.reset_signals


# ---------------------------------------------------------------------------
# hierarchy.sv
# ---------------------------------------------------------------------------

class TestParseHierarchy:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("hierarchy.sv")])

    def test_both_modules_parsed(self):
        assert "logic_gate" in self.db.modules
        assert "hierarchy_top" in self.db.modules

    def test_logic_gate_has_ports(self):
        mod = self.db.modules["logic_gate"]
        assert set(mod.ports.keys()) == {"a", "b", "y"}

    def test_logic_gate_port_directions(self):
        mod = self.db.modules["logic_gate"]
        assert mod.ports["a"].direction == "input"
        assert mod.ports["b"].direction == "input"
        assert mod.ports["y"].direction == "output"

    def test_hierarchy_top_has_two_instances(self):
        mod = self.db.modules["hierarchy_top"]
        assert len(mod.instances) == 2

    def test_u0_module_type(self):
        mod = self.db.modules["hierarchy_top"]
        u0 = next(i for i in mod.instances if i.name == "u0")
        assert u0.module_type == "logic_gate"

    def test_u0_port_connections(self):
        mod = self.db.modules["hierarchy_top"]
        u0 = next(i for i in mod.instances if i.name == "u0")
        assert u0.connections["a"] == "a"
        assert u0.connections["b"] == "b"
        assert u0.connections["y"] == "mid"

    def test_u1_port_connections(self):
        mod = self.db.modules["hierarchy_top"]
        u1 = next(i for i in mod.instances if i.name == "u1")
        assert u1.connections["a"] == "mid"
        assert u1.connections["b"] == "c"
        assert u1.connections["y"] == "out_ab"


# ---------------------------------------------------------------------------
# multiport.sv
# ---------------------------------------------------------------------------

class TestParseMultiport:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("multiport.sv")])
        self.mod = self.db.modules["multiport"]

    def test_module_found(self):
        assert "multiport" in self.db.modules

    def test_ports_present(self):
        expected = {"clk", "rst_n", "addr", "wdata", "we", "rdata", "valid"}
        assert expected.issubset(set(self.mod.ports.keys()))

    def test_clk_is_input(self):
        assert self.mod.ports["clk"].direction == "input"

    def test_rdata_is_output(self):
        assert self.mod.ports["rdata"].direction == "output"

    def test_always_ff_present(self):
        ff = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff) >= 1

    def test_two_assign_statements(self):
        # assign rdata = rdata_r; assign valid = valid_r;
        assert len(self.mod.assignments) >= 2


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------

class TestParseEdgeCases:
    def test_nonexistent_file_yields_empty_db(self):
        db = parse_sv_files(["/nonexistent/path/no_such_file.sv"])
        assert len(db.modules) == 0

    def test_empty_file_list(self):
        db = parse_sv_files([])
        assert isinstance(db, DesignDB)
        assert len(db.modules) == 0
