from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import rtlens.netlistsvg_view as netlistsvg_view
import rtlens.slang_backend as slang_backend


def test_yosys_quote_arg_avoids_single_quote_shell_style() -> None:
    assert netlistsvg_view._yosys_quote_arg("-IC:/tmp/src") == "-IC:/tmp/src"
    quoted = netlistsvg_view._yosys_quote_arg("C:/Program Files/project/src/a.sv")
    assert quoted == '"C:/Program Files/project/src/a.sv"'
    assert "'" not in quoted


def test_translate_slang_args_to_yosys_normalizes_windows_paths(monkeypatch) -> None:
    monkeypatch.setattr(netlistsvg_view.os, "name", "nt", raising=False)
    out = netlistsvg_view._translate_slang_args_to_yosys(
        [
            "+incdir+C:\\rtl\\inc",
            "-I",
            "D:\\common\\inc",
            "-IE:\\third_party\\inc",
            "+define+SIM=1",
        ]
    )
    assert out == [
        "-IC:/rtl/inc",
        "-I",
        "D:/common/inc",
        "-IE:/third_party/inc",
        "-DSIM=1",
    ]


def test_slang_backend_windows_arg_helpers(monkeypatch) -> None:
    monkeypatch.setattr(slang_backend.os, "name", "nt", raising=False)
    args = [
        "+incdir+C:\\rtl\\inc",
        "-I",
        "D:\\common\\inc",
        "-IE:\\third_party\\inc",
        "+define+SIM=1",
        "-D",
        "FLAG=1",
        "-DFEATURE=1",
    ]
    norm = slang_backend._normalize_windows_slang_args(args)
    assert norm == [
        "+incdir+C:/rtl/inc",
        "-I",
        "D:/common/inc",
        "-IE:/third_party/inc",
        "+define+SIM=1",
        "-D",
        "FLAG=1",
        "-DFEATURE=1",
    ]
    dropped = slang_backend._drop_include_args(norm)
    assert dropped == ["+define+SIM=1", "-D", "FLAG=1", "-DFEATURE=1"]


def test_windows_access_violation_codes() -> None:
    assert slang_backend._is_windows_access_violation(3221225477)
    assert slang_backend._is_windows_access_violation(-1073741819)
    assert slang_backend._is_windows_access_violation(3221225620)
    assert slang_backend._is_windows_access_violation(-1073741676)
    assert not slang_backend._is_windows_access_violation(1)


def test_build_slang_dump_args_with_stage() -> None:
    args = slang_backend._build_slang_dump_args(
        tool=Path("/tmp/slang_dump"),
        top="topmod",
        extra_args=["+incdir+src", "-DFLAG=1"],
        abs_files=["/tmp/a.sv", "/tmp/b.sv"],
        stage="ports",
    )
    assert args == [
        str(Path("/tmp/slang_dump")),
        "--rtlens-top",
        "topmod",
        "--rtlens-stage",
        "ports",
        "+incdir+src",
        "-DFLAG=1",
        "/tmp/a.sv",
        "/tmp/b.sv",
    ]


def test_windows_stage_probe_order_has_hier_substages() -> None:
    assert slang_backend._WINDOWS_SVVIEW_STAGES[:3] == [
        "hier-scan",
        "hier-visit",
        "hier-defs",
    ]


def test_windows_toolchain_mismatch_lines_detects_runtime_diff(monkeypatch) -> None:
    monkeypatch.delenv("SVVIEW_CMAKE_GENERATOR", raising=False)
    monkeypatch.delenv("SVVIEW_CXX_COMPILER", raising=False)
    monkeypatch.delenv("SVVIEW_CMAKE_MAKE_PROGRAM", raising=False)
    meta = {
        "meta_path": r"C:\tmp\slang_toolchain_meta.json",
        "cmake_generator": "MinGW Makefiles",
        "cxx_compiler": r"C:\msys64\ucrt64\bin\g++.exe",
        "make_program": r"C:\msys64\ucrt64\bin\mingw32-make.exe",
    }
    runtime = {
        "resolved": {
            "g++": r"C:\llvm\bin\clang++.exe",
            "mingw32-make": r"C:\msys64\mingw64\bin\mingw32-make.exe",
            "ninja": "",
            "nmake": "",
        }
    }
    lines = slang_backend._windows_toolchain_mismatch_lines(meta, runtime)
    assert any("toolchain warning" in line for line in lines)
    assert any("metadata source" in line for line in lines)


def test_minimize_windows_crash_inputs_reduces_to_bad_file(monkeypatch) -> None:
    def fake_run(args, run_env):  # noqa: ARG001
        files = [str(a) for a in args if str(a).endswith(".sv")]
        crash = any("bad.sv" in f for f in files)
        return SimpleNamespace(returncode=(3221225477 if crash else 0), stdout="", stderr="")

    monkeypatch.setattr(slang_backend, "_run_slang_dump", fake_run)
    minset, attempts = slang_backend._minimize_windows_crash_inputs(
        tool=Path("/tmp/slang_dump.exe"),
        top="top",
        normalized_extra=["+incdir+src"],
        abs_files=["/tmp/a.sv", "/tmp/bad.sv", "/tmp/c.sv"],
        run_env={},
        stage="hier-defs",
    )
    assert minset == ["/tmp/bad.sv"]
    assert attempts
