#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[2]
SVVIEW_DIR = REPO / "rtlens"


@dataclass(frozen=True)
class GuiCaseSpec:
    name: str
    filelist: Path
    top: str
    prebuild_top: str


CASES: Dict[str, GuiCaseSpec] = {
    "min_case": GuiCaseSpec(
        name="min_case",
        filelist=REPO / "RTL" / "verification" / "min_case" / "vlist",
        top="vm_min_top",
        prebuild_top="vm_min_top",
    ),
    "mid_case": GuiCaseSpec(
        name="mid_case",
        filelist=REPO / "RTL" / "verification" / "mid_case" / "vlist",
        top="vm_mid_top",
        prebuild_top="vm_mid_top",
    ),
    "deep_case": GuiCaseSpec(
        name="deep_case",
        filelist=REPO / "RTL" / "verification" / "deep_case" / "vlist",
        top="vm_deep_top",
        prebuild_top="vm_deep_top",
    ),
}


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _build_case_argv(case: GuiCaseSpec, python_cmd: str, log_level: str, extra_args: List[str]) -> List[str]:
    argv = [
        python_cmd,
        "-m",
        "rtlens",
        "--ui",
        "qt",
        "--filelist",
        str(case.filelist),
        "--top",
        case.top,
        "--schematic-prebuild-top",
        case.prebuild_top,
        "--schematic-view",
        "svg",
        "--schematic-prebuild-log-level",
        log_level,
    ]
    argv.extend(extra_args)
    return argv


def _shell_line(argv: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prepare/run Qt GUI regression sessions for verification RTL cases")
    p.add_argument(
        "--case",
        action="append",
        default=[],
        choices=["min_case", "mid_case", "deep_case", "all"],
        help="target case name (repeatable). default: all",
    )
    p.add_argument(
        "--mode",
        choices=["print", "run"],
        default="print",
        help="print commands only, or launch one Qt session",
    )
    p.add_argument("--python-cmd", default=sys.executable, help="python executable for launching RTLens")
    p.add_argument(
        "--schematic-prebuild-log-level",
        default="phase",
        choices=["phase", "detail", "quiet"],
        help="prebuild log level passed to RTLens",
    )
    p.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="additional argument forwarded to RTLens (repeatable)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    selected = args.case or ["all"]
    if "all" in selected:
        run_names = sorted(CASES.keys())
    else:
        seen = set()
        run_names = []
        for name in selected:
            if name in seen:
                continue
            seen.add(name)
            run_names.append(name)

    commands = []
    for name in run_names:
        case = CASES[name]
        if not case.filelist.is_file():
            return _fail(f"{name}: missing filelist {case.filelist}")
        commands.append((name, _build_case_argv(case, args.python_cmd, args.schematic_prebuild_log_level, args.extra_arg)))

    if args.mode == "print":
        print(f"# cwd: {SVVIEW_DIR}")
        for name, argv in commands:
            print(f"[{name}]")
            print(_shell_line(argv))
        _pass(f"printed {len(commands)} command(s)")
        return 0

    if len(commands) != 1:
        return _fail("--mode run accepts exactly one case (use --case <name>)")

    name, argv = commands[0]
    print(f"[RUN] case={name}")
    print(_shell_line(argv))
    rc = subprocess.run(argv, cwd=str(SVVIEW_DIR), check=False).returncode
    if rc != 0:
        return _fail(f"{name}: RTLens exited with code {rc}")
    _pass(f"{name}: session exited successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
