"""Unit tests for netlistsvg SVG source-normalization helpers."""
from __future__ import annotations

from rtlens.netlistsvg_svg import _normalize_svg_src_for_ui


def test_normalize_svg_src_for_ui_prefers_existing_file(tmp_path):
    src_file = tmp_path / "rtl.sv"
    src_file.write_text("module m; endmodule\n", encoding="utf-8")
    src = f"{src_file}:7"
    assert _normalize_svg_src_for_ui(src, fallback="fallback.sv:1") == src


def test_normalize_svg_src_for_ui_drops_generated_tmp_paths():
    samples = [
        "/tmp/rtlens_netlistsvg_x/tmp.sv:8",
        "/private/tmp/rtlens_schematic_prebuild_abc/main.sv:4",
        r"C:\Users\foo\AppData\Local\Temp\rtlens_netlistsvg_x\tmp.sv:22",
    ]
    for raw in samples:
        assert _normalize_svg_src_for_ui(raw, fallback="real.sv:11") == "real.sv:11"
