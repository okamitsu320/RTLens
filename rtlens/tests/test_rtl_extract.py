"""Tests for rtl_extract.extract_module_structure() using sv_parser fixtures."""
from __future__ import annotations

import pytest

from rtlens.sv_parser import parse_sv_files
from rtlens.connectivity import build_hierarchy
from rtlens.rtl_extract import extract_module_structure

from .conftest import fixture_path


# ---------------------------------------------------------------------------
# simple_assign.sv
# ---------------------------------------------------------------------------

class TestSimpleAssign:
    def setup_method(self):
        db = parse_sv_files([fixture_path("simple_assign.sv")])
        build_hierarchy(db, None)
        self.struct = extract_module_structure(db, "simple_assign")

    def test_module_name(self):
        assert self.struct.module_name == "simple_assign"

    def test_port_count(self):
        assert len(self.struct.module_ports) == 5

    def test_port_directions(self):
        by_name = {p.name: p.direction for p in self.struct.module_ports}
        assert by_name["a"] == "input"
        assert by_name["b"] == "input"
        assert by_name["sel"] == "input"
        assert by_name["y"] == "output"
        assert by_name["eq"] == "output"

    def test_signal_ids_are_sig_prefixed(self):
        ids = {s.id for s in self.struct.signals}
        assert "sig_a" in ids
        assert "sig_y" in ids
        assert "sig_eq" in ids

    def test_input_signal_has_input_tag(self):
        a_sig = next(s for s in self.struct.signals if s.name == "a")
        assert "input" in a_sig.tags

    def test_output_signal_has_output_tag(self):
        y_sig = next(s for s in self.struct.signals if s.name == "y")
        assert "output" in y_sig.tags

    def test_assign_count(self):
        # Two assign statements: y = sel?a:b and eq = (a==b)
        assert len(self.struct.assigns) == 2

    def test_assign_y_output_signal(self):
        y_assign = next(a for a in self.struct.assigns if "sig_y" in a.output_signals)
        assert "sig_y" in y_assign.output_signals

    def test_assign_y_input_signals(self):
        y_assign = next(a for a in self.struct.assigns if "sig_y" in a.output_signals)
        assert "sig_sel" in y_assign.input_signals
        assert "sig_a" in y_assign.input_signals
        assert "sig_b" in y_assign.input_signals

    def test_assign_eq_inputs(self):
        eq_assign = next(a for a in self.struct.assigns if "sig_eq" in a.output_signals)
        assert "sig_a" in eq_assign.input_signals
        assert "sig_b" in eq_assign.input_signals

    def test_no_instances(self):
        assert len(self.struct.instances) == 0

    def test_no_always_blocks(self):
        assert len(self.struct.always_blocks) == 0


# ---------------------------------------------------------------------------
# counter.sv
# ---------------------------------------------------------------------------

class TestCounter:
    def setup_method(self):
        db = parse_sv_files([fixture_path("counter.sv")])
        build_hierarchy(db, None)
        self.struct = extract_module_structure(db, "counter")
        self.db = db

    def test_module_name(self):
        assert self.struct.module_name == "counter"

    def test_ports(self):
        port_names = {p.name for p in self.struct.module_ports}
        assert port_names == {"clk", "rst_n", "en", "q"}

    def test_clk_has_clock_tag(self):
        clk = next(s for s in self.struct.signals if s.name == "clk")
        assert "clock" in clk.tags

    def test_rst_n_has_reset_tag(self):
        rst = next(s for s in self.struct.signals if s.name == "rst_n")
        assert "reset" in rst.tags

    def test_en_has_no_clock_reset_tag(self):
        en = next(s for s in self.struct.signals if s.name == "en")
        assert "clock" not in en.tags
        assert "reset" not in en.tags

    def test_internal_signal_cnt_present(self):
        names = {s.name for s in self.struct.signals}
        assert "cnt" in names

    def test_cnt_has_internal_tag(self):
        cnt = next(s for s in self.struct.signals if s.name == "cnt")
        assert "internal" in cnt.tags

    def test_always_ff_block_present(self):
        ff_blocks = [b for b in self.struct.always_blocks if b.always_kind == "always_ff"]
        assert len(ff_blocks) == 1

    def test_always_ff_clock_signal(self):
        ff = next(b for b in self.struct.always_blocks if b.always_kind == "always_ff")
        # ExtractedAlways stores signal IDs (sig_<name>)
        assert "sig_clk" in ff.clock_signals

    def test_always_ff_reset_signal(self):
        ff = next(b for b in self.struct.always_blocks if b.always_kind == "always_ff")
        assert "sig_rst_n" in ff.reset_signals


# ---------------------------------------------------------------------------
# hierarchy.sv — hierarchy_top
# ---------------------------------------------------------------------------

class TestHierarchyTop:
    def setup_method(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        self.struct = extract_module_structure(db, "hierarchy_top")
        self.db = db

    def test_module_name(self):
        assert self.struct.module_name == "hierarchy_top"

    def test_instance_count(self):
        assert len(self.struct.instances) == 2

    def test_instance_names(self):
        names = {i.name for i in self.struct.instances}
        assert names == {"u0", "u1"}

    def test_instance_module_types(self):
        types = {i.module_name for i in self.struct.instances}
        assert types == {"logic_gate"}

    def test_u0_port_connections(self):
        u0 = next(i for i in self.struct.instances if i.name == "u0")
        port_map = {p.name: p for p in u0.ports}
        # Port 'a' connects to parent signal 'a'
        assert "a" in port_map
        # Port 'y' connects to parent signal 'mid'
        assert "y" in port_map

    def test_internal_signal_mid(self):
        names = {s.name for s in self.struct.signals}
        assert "mid" in names

    def test_assign_out_abc(self):
        assigns_with_out_abc = [a for a in self.struct.assigns if "sig_out_abc" in a.output_signals]
        assert len(assigns_with_out_abc) == 1

    def test_hier_has_child_paths(self):
        assert "hierarchy_top.u0" in self.db.hier
        assert "hierarchy_top.u1" in self.db.hier


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestExtractErrors:
    def test_missing_hier_path_raises_key_error(self):
        db = parse_sv_files([fixture_path("simple_assign.sv")])
        build_hierarchy(db, None)
        with pytest.raises(KeyError):
            extract_module_structure(db, "nonexistent_path")
