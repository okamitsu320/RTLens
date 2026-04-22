from __future__ import annotations

from pathlib import Path

import pytest

from rtlens.editor_cmd import build_editor_argv


def test_build_editor_argv_expands_basename_and_dir(tmp_path: Path) -> None:
    src = tmp_path / "sub dir" / "main file.sv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("module main; endmodule\n", encoding="utf-8")

    argv = build_editor_argv("echo {basename} {dir} {line}", str(src), 17)

    assert argv[0] == "echo"
    assert argv[1] == src.name
    assert argv[2] == str(src.parent.resolve())
    assert argv[3] == "17"


def test_build_editor_argv_keeps_existing_placeholders_compatible(tmp_path: Path) -> None:
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n", encoding="utf-8")

    argv = build_editor_argv("code --goto {file}:{line}", str(src), 9)

    assert argv == ["code", "--goto", f"{src}:{9}"]


def test_build_editor_argv_rejects_unknown_placeholder(tmp_path: Path) -> None:
    src = tmp_path / "a.sv"
    src.write_text("module a; endmodule\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported placeholder"):
        build_editor_argv("echo {unknown}", str(src), 1)
