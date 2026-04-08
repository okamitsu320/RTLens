"""Unit tests for Qt-independent text/path helpers extracted from qt_app."""
from __future__ import annotations

from rtlens.qt_text_utils import (
    canonical_schematic_name,
    classify_schematic_cell_type,
    cleanup_wave_name,
    demangle_paramod_module_name,
    extract_wave_name_candidates,
    normalize_schematic_src,
    parse_jump_item,
)


def test_canonical_schematic_name_strips_leading_backslashes():
    assert canonical_schematic_name(r"\foo") == "foo"
    assert canonical_schematic_name(r"\\foo") == "foo"


def test_demangle_paramod_module_name():
    assert demangle_paramod_module_name("$paramod$my_mod$abc=1") == "my_mod"
    assert demangle_paramod_module_name(r"$paramod\vm_deep_stage\ID=s32'0001") == "vm_deep_stage"
    assert demangle_paramod_module_name("$and") == ""


def test_classify_schematic_cell_type():
    assert classify_schematic_cell_type("my_mod") == ("instance", "my_mod")
    assert classify_schematic_cell_type("$and") == ("cell", "$and")
    assert classify_schematic_cell_type("$paramod$my_mod$WIDTH=8") == ("instance", "my_mod")
    assert classify_schematic_cell_type(r"$paramod\vm_mid_lane\LANE=s32'1") == ("instance", "vm_mid_lane")


def test_normalize_schematic_src_prefers_existing_file(tmp_path):
    src_file = tmp_path / "rtl.sv"
    src_file.write_text("module m; endmodule\n", encoding="utf-8")
    src = f"{src_file}:42"
    assert normalize_schematic_src(src, fallback="fallback.sv:1") == src


def test_normalize_schematic_src_drops_tmp_generated_paths():
    samples = [
        "/tmp/rtlens_netlistsvg_x/tmp.sv:8",
        "/private/tmp/rtlens_schematic_prebuild_abc/main.sv:4",
        r"C:\Users\foo\AppData\Local\Temp\rtlens_netlistsvg_x\tmp.sv:22",
    ]
    for raw in samples:
        assert normalize_schematic_src(raw, fallback="real.sv:11") == "real.sv:11"


def test_cleanup_wave_name_removes_brackets_and_slashes():
    assert cleanup_wave_name(" '/top/u0/data[3:0]' ") == "top.u0.data"


def test_extract_wave_name_candidates_dedupes_preserves_order():
    text = "top.u0.sig\nfoo bar\ntop/u0/sig\nfoo"
    assert extract_wave_name_candidates(text) == ["top.u0.sig", "foo", "top/u0/sig", "bar"]


def test_parse_jump_item_valid():
    assert parse_jump_item("top.u0.sig -> /tmp/a.sv:17") == ("/tmp/a.sv", 17, "sig", "top.u0.sig")


def test_parse_jump_item_invalid():
    assert parse_jump_item("not-a-jump") is None
    assert parse_jump_item("sig -> /tmp/a.sv:bad") is None
