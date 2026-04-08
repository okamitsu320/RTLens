"""CLI parser tests for UI backend defaults and compatibility."""
from __future__ import annotations

from rtlens.app_cli import build_arg_parser


def test_ui_default_is_qt():
    args = build_arg_parser().parse_args([])
    assert args.ui == "qt"


def test_ui_tk_is_still_selectable():
    args = build_arg_parser().parse_args(["--ui", "tk"])
    assert args.ui == "tk"
