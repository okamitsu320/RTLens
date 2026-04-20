"""Tests for connectivity.build_hierarchy() and build_connectivity() / query_signal()."""
from __future__ import annotations

from rtlens.sv_parser import parse_sv_files
from rtlens.connectivity import build_hierarchy, build_connectivity, query_signal

from .conftest import fixture_path


# ---------------------------------------------------------------------------
# build_hierarchy
# ---------------------------------------------------------------------------

class TestBuildHierarchy:
    def test_simple_assign_single_root(self):
        db = parse_sv_files([fixture_path("simple_assign.sv")])
        build_hierarchy(db, None)
        assert db.roots == ["simple_assign"]
        assert db.top_module == "simple_assign"

    def test_simple_assign_hier_has_one_node(self):
        db = parse_sv_files([fixture_path("simple_assign.sv")])
        build_hierarchy(db, None)
        assert list(db.hier.keys()) == ["simple_assign"]

    def test_hierarchy_top_has_two_children(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        node = db.hier["hierarchy_top"]
        assert set(node.children) == {"hierarchy_top.u0", "hierarchy_top.u1"}

    def test_hierarchy_top_is_root(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        assert db.roots == ["hierarchy_top"]
        assert db.top_module == "hierarchy_top"

    def test_child_module_name_is_logic_gate(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        assert db.hier["hierarchy_top.u0"].module_name == "logic_gate"
        assert db.hier["hierarchy_top.u1"].module_name == "logic_gate"

    def test_child_parent_is_top(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        assert db.hier["hierarchy_top.u0"].parent == "hierarchy_top"

    def test_explicit_top_module_overrides_inference(self):
        # logic_gate is instantiated, so it would not be inferred as root
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "logic_gate")
        assert db.top_module == "logic_gate"

    def test_empty_db_does_not_crash(self):
        from rtlens.model import DesignDB
        db = DesignDB()
        build_hierarchy(db, None)  # should not raise
        assert db.roots == []


# ---------------------------------------------------------------------------
# build_connectivity
# ---------------------------------------------------------------------------

class TestBuildConnectivity:
    def setup_method(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        self.cdb = build_connectivity(db)

    def test_top_input_drives_child_port(self):
        # hierarchy_top.a → hierarchy_top.u0.a (input port connection)
        assert "hierarchy_top.u0.a" in self.cdb.drives_data.get("hierarchy_top.a", set())

    def test_child_output_drives_top_signal(self):
        # u0.y (output) → hierarchy_top.mid
        assert "hierarchy_top.mid" in self.cdb.drives_data.get("hierarchy_top.u0.y", set())

    def test_mid_drives_u1_input(self):
        # hierarchy_top.mid → hierarchy_top.u1.a
        assert "hierarchy_top.u1.a" in self.cdb.drives_data.get("hierarchy_top.mid", set())

    def test_assign_edge_mid_to_out_abc(self):
        # assign out_abc = mid & out_ab → mid drives out_abc
        assert "hierarchy_top.out_abc" in self.cdb.drives_data.get("hierarchy_top.mid", set())

    def test_signal_to_source_has_top_signals(self):
        for sig in ["hierarchy_top.a", "hierarchy_top.b", "hierarchy_top.mid"]:
            assert sig in self.cdb.signal_to_source


# ---------------------------------------------------------------------------
# query_signal
# ---------------------------------------------------------------------------

class TestQuerySignal:
    def setup_method(self):
        db = parse_sv_files([fixture_path("hierarchy.sv")])
        build_hierarchy(db, "hierarchy_top")
        self.cdb = build_connectivity(db)

    def test_out_ab_recursive_drivers_include_u1_y(self):
        drivers, _ = query_signal(self.cdb, "hierarchy_top.out_ab", recursive=True)
        driver_names = {s for s, _ in drivers}
        assert "hierarchy_top.u1.y" in driver_names

    def test_out_ab_recursive_drivers_include_mid(self):
        drivers, _ = query_signal(self.cdb, "hierarchy_top.out_ab", recursive=True)
        driver_names = {s for s, _ in drivers}
        assert "hierarchy_top.mid" in driver_names

    def test_out_ab_recursive_drivers_trace_to_inputs(self):
        drivers, _ = query_signal(self.cdb, "hierarchy_top.out_ab", recursive=True)
        driver_names = {s for s, _ in drivers}
        # Should reach the top-level inputs through u0/u1
        assert "hierarchy_top.a" in driver_names
        assert "hierarchy_top.b" in driver_names
        assert "hierarchy_top.c" in driver_names

    def test_out_ab_recursive_loads_include_out_abc(self):
        _, loads = query_signal(self.cdb, "hierarchy_top.out_ab", recursive=True)
        load_names = {s for s, _ in loads}
        assert "hierarchy_top.out_abc" in load_names

    def test_mid_recursive_drivers_include_u0_y(self):
        drivers, _ = query_signal(self.cdb, "hierarchy_top.mid", recursive=True)
        driver_names = {s for s, _ in drivers}
        assert "hierarchy_top.u0.y" in driver_names

    def test_query_nonexistent_signal_returns_empty(self):
        drivers, loads = query_signal(self.cdb, "hierarchy_top.does_not_exist", recursive=True)
        assert drivers == []
        assert loads == []

    def test_mid_direct_query_with_ports_has_sites(self):
        drivers, loads = query_signal(
            self.cdb,
            "hierarchy_top.mid",
            recursive=False,
            include_control=False,
            include_ports=True,
        )
        assert len(drivers) >= 1
        assert len(loads) >= 1


class TestQuerySignalFallbackProcedural:
    def setup_method(self):
        db = parse_sv_files([fixture_path("counter.sv")])
        build_hierarchy(db, "counter")
        self.cdb = build_connectivity(db)

    def test_counter_cnt_direct_query_has_driver_site(self):
        drivers, _loads = query_signal(
            self.cdb,
            "counter.cnt",
            recursive=False,
            include_control=True,
            include_ports=False,
        )
        assert len(drivers) >= 1

    def test_counter_cnt_direct_query_has_driver_and_load(self):
        drivers, loads = query_signal(
            self.cdb,
            "counter.cnt",
            recursive=False,
            include_control=True,
            include_ports=True,
        )
        assert len(drivers) >= 1
        assert len(loads) >= 1

    def test_clock_dependency_toggle_for_counter_clk(self):
        _drivers0, loads0 = query_signal(
            self.cdb,
            "counter.clk",
            recursive=False,
            include_control=False,
            include_clock=False,
            include_ports=False,
        )
        _drivers1, loads1 = query_signal(
            self.cdb,
            "counter.clk",
            recursive=False,
            include_control=False,
            include_clock=True,
            include_ports=False,
        )
        assert len(loads0) == 0
        assert len(loads1) >= 1
