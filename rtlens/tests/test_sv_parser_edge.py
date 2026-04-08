"""Edge-case tests for sv_parser: FSM, pipeline, ifdef guards, generate-for.

These tests exercise parser behaviour on the four additional fixtures added in
the RTLens roadmap Phase-1 work.  No external binaries are required.
"""
from __future__ import annotations

import pytest

from rtlens.sv_parser import parse_sv_files
from .conftest import fixture_path


# ---------------------------------------------------------------------------
# fsm.sv — always_ff + always_comb, typedef enum
# ---------------------------------------------------------------------------

class TestParseFsm:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("fsm.sv")])
        self.mod = self.db.modules["fsm"]

    def test_module_found(self):
        assert "fsm" in self.db.modules

    def test_port_names(self):
        assert set(self.mod.ports.keys()) == {"clk", "rst_n", "start", "done", "busy", "ready"}

    def test_clk_is_input(self):
        assert self.mod.ports["clk"].direction == "input"

    def test_busy_is_output(self):
        assert self.mod.ports["busy"].direction == "output"

    def test_always_ff_present(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff_blocks) == 1

    def test_always_comb_present(self):
        comb_blocks = [b for b in self.mod.always_blocks if b.kind == "always_comb"]
        assert len(comb_blocks) == 1

    def test_always_ff_clock_signal(self):
        ff = next(b for b in self.mod.always_blocks if b.kind == "always_ff")
        assert "clk" in ff.clock_signals

    def test_always_ff_reset_signal(self):
        ff = next(b for b in self.mod.always_blocks if b.kind == "always_ff")
        assert "rst_n" in ff.reset_signals

    def test_always_ff_writes_state(self):
        ff = next(b for b in self.mod.always_blocks if b.kind == "always_ff")
        assert "state" in ff.writes

    def test_always_comb_writes_busy(self):
        cb = next(b for b in self.mod.always_blocks if b.kind == "always_comb")
        assert "busy" in cb.writes

    def test_always_comb_writes_ready(self):
        cb = next(b for b in self.mod.always_blocks if b.kind == "always_comb")
        assert "ready" in cb.writes

    def test_no_instances(self):
        assert len(self.mod.instances) == 0


# ---------------------------------------------------------------------------
# pipeline.sv — two always_ff blocks, inter-stage registers
# ---------------------------------------------------------------------------

class TestParsePipeline:
    def setup_method(self):
        self.db = parse_sv_files([fixture_path("pipeline.sv")])
        self.mod = self.db.modules["pipeline"]

    def test_module_found(self):
        assert "pipeline" in self.db.modules

    def test_port_names(self):
        expected = {"clk", "rst_n", "instr_in", "valid_in", "instr_out", "valid_out"}
        assert set(self.mod.ports.keys()) == expected

    def test_two_always_ff_blocks(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff_blocks) == 2

    def test_both_ff_blocks_have_clock(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        for blk in ff_blocks:
            assert "clk" in blk.clock_signals

    def test_both_ff_blocks_have_reset(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        for blk in ff_blocks:
            assert "rst_n" in blk.reset_signals

    def test_stage1_registers_declared(self):
        assert "s1_instr" in self.mod.signals
        assert "s1_valid" in self.mod.signals

    def test_stage2_registers_declared(self):
        assert "s2_instr" in self.mod.signals
        assert "s2_valid" in self.mod.signals

    def test_two_assign_statements(self):
        # assign instr_out = s2_instr;  assign valid_out = s2_valid;
        assert len(self.mod.assignments) >= 2

    def test_assign_instr_out_lhs(self):
        lhses = [a.lhs for a in self.mod.assignments]
        assert ["instr_out"] in lhses

    def test_no_instances(self):
        assert len(self.mod.instances) == 0


# ---------------------------------------------------------------------------
# ifdef_guard.sv — conditional compilation with defined_macros
# ---------------------------------------------------------------------------

class TestParseIfdefGuardDefault:
    """Without USE_FAST_PATH: always_ff registered path should be active."""

    def setup_method(self):
        # No macro defined → `else branch is active
        self.db = parse_sv_files([fixture_path("ifdef_guard.sv")])
        self.mod = self.db.modules["ifdef_guard"]

    def test_module_found(self):
        assert "ifdef_guard" in self.db.modules

    def test_always_ff_present_in_default_path(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff_blocks) == 1

    def test_data_r_signal_present_in_default_path(self):
        assert "data_r" in self.mod.signals

    def test_assign_data_out_present_in_default_path(self):
        lhses = [a.lhs for a in self.mod.assignments]
        assert ["data_out"] in lhses


class TestParseIfdefGuardWithMacro:
    """With USE_FAST_PATH defined: combinational path should be active."""

    def setup_method(self):
        self.db = parse_sv_files(
            [fixture_path("ifdef_guard.sv")],
            defined_macros={"USE_FAST_PATH"},
        )
        self.mod = self.db.modules["ifdef_guard"]

    def test_module_found(self):
        assert "ifdef_guard" in self.db.modules

    def test_no_always_ff_in_fast_path(self):
        ff_blocks = [b for b in self.mod.always_blocks if b.kind == "always_ff"]
        assert len(ff_blocks) == 0

    def test_no_data_r_in_fast_path(self):
        # data_r is only declared in the `else branch
        assert "data_r" not in self.mod.signals

    def test_assign_data_out_still_present(self):
        lhses = [a.lhs for a in self.mod.assignments]
        assert ["data_out"] in lhses


# ---------------------------------------------------------------------------
# generate_for.sv — generate-for (parser limitation)
# ---------------------------------------------------------------------------

class TestParseGenerateFor:
    """Verify the parser does not crash on generate-for and correctly identifies
    the module boundary and ports."""

    def setup_method(self):
        self.db = parse_sv_files([fixture_path("generate_for.sv")])
        self.mod = self.db.modules["generate_for"]

    def test_module_found(self):
        assert "generate_for" in self.db.modules

    def test_port_names(self):
        assert set(self.mod.ports.keys()) == {"in_vec", "out_vec"}

    def test_in_vec_is_input(self):
        assert self.mod.ports["in_vec"].direction == "input"

    def test_out_vec_is_output(self):
        assert self.mod.ports["out_vec"].direction == "output"

    def test_no_instances(self):
        # generate-for does not create InstanceDef records in the fallback parser
        assert len(self.mod.instances) == 0

    def test_no_always_blocks(self):
        assert len(self.mod.always_blocks) == 0

    def test_parser_does_not_crash(self):
        # Regression guard: parsing must complete without raising an exception
        db2 = parse_sv_files([fixture_path("generate_for.sv")])
        assert "generate_for" in db2.modules
