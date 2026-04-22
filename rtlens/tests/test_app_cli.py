"""CLI parser tests for UI backend defaults and compatibility."""
from __future__ import annotations

from rtlens.app_cli import _collect_rtl_inputs_from_args, build_arg_parser


def test_ui_default_is_qt():
    args = build_arg_parser().parse_args([])
    assert args.ui == "qt"


def test_ui_tk_is_still_selectable():
    args = build_arg_parser().parse_args(["--ui", "tk"])
    assert args.ui == "tk"


def test_editor_cmd_default_is_none_for_cli_omission_detection():
    args = build_arg_parser().parse_args([])
    assert args.editor_cmd is None


def test_editor_cmd_accepts_explicit_template():
    args = build_arg_parser().parse_args(["--editor-cmd", "code --goto {file}:{line}"])
    assert args.editor_cmd == "code --goto {file}:{line}"


def test_collect_rtl_inputs_accepts_rtl_file_only(tmp_path):
    rtl = tmp_path / "one.sv"
    rtl.write_text("module one; endmodule\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--rtl-file", str(rtl), "--top", "one"])
    files, slang_args, filelists_used = _collect_rtl_inputs_from_args(args)
    assert files == [str(rtl.resolve())]
    assert slang_args == []
    assert filelists_used == []


def test_collect_rtl_inputs_prefers_filelist_then_rtl_file(tmp_path):
    via_fl = tmp_path / "via_fl.sv"
    via_fl.write_text("module via_fl; endmodule\n", encoding="utf-8")
    direct = tmp_path / "direct.sv"
    direct.write_text("module direct; endmodule\n", encoding="utf-8")
    fl = tmp_path / "vlist.f"
    fl.write_text(str(via_fl) + "\n", encoding="utf-8")
    args = build_arg_parser().parse_args(
        [
            "--filelist",
            str(fl),
            "--rtl-file",
            str(direct),
        ]
    )
    files, _slang_args, _filelists_used = _collect_rtl_inputs_from_args(args)
    assert files == [str(via_fl.resolve()), str(direct.resolve())]
