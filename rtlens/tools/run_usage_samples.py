#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SampleCase:
    name: str
    filelist: Path
    top: str


CASES = {
    "min_case": SampleCase(
        name="min_case",
        filelist=REPO / "RTL" / "verification" / "min_case" / "vlist",
        top="vm_min_top",
    ),
    "mid_case": SampleCase(
        name="mid_case",
        filelist=REPO / "RTL" / "verification" / "mid_case" / "vlist",
        top="vm_mid_top",
    ),
    "deep_case": SampleCase(
        name="deep_case",
        filelist=REPO / "RTL" / "verification" / "deep_case" / "vlist",
        top="vm_deep_top",
    ),
}


def _build_command(python_cmd: str, case: SampleCase, ui: str, extra_args: list[str]) -> list[str]:
    cmd = [
        python_cmd,
        "-m",
        "rtlens",
        "--ui",
        ui,
        "--filelist",
        str(case.filelist),
        "--top",
        case.top,
    ]
    cmd.extend(extra_args)
    return cmd


def _iter_cases(case_name: str) -> list[SampleCase]:
    if case_name == "all":
        return [CASES["min_case"], CASES["mid_case"], CASES["deep_case"]]
    return [CASES[case_name]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print or run canonical RTLens usage sample commands.")
    parser.add_argument("--mode", choices=["print", "run"], default="print")
    parser.add_argument("--case", choices=["min_case", "mid_case", "deep_case", "all"], default="all")
    parser.add_argument("--ui", choices=["qt", "tk"], default="qt")
    parser.add_argument("--python-cmd", default=sys.executable)
    parser.add_argument("--extra-arg", action="append", default=[])
    args = parser.parse_args(argv)

    cases = _iter_cases(args.case)
    for case in cases:
        cmd = _build_command(
            python_cmd=args.python_cmd,
            case=case,
            ui=args.ui,
            extra_args=list(args.extra_arg),
        )
        print(f"[SAMPLE] case={case.name}")
        print(" ".join(subprocess.list2cmdline([part]) for part in cmd))
        if args.mode == "run":
            completed = subprocess.run(cmd, cwd=REPO)
            if completed.returncode != 0:
                print(f"[FAIL] case={case.name}: rtlens exited with code {completed.returncode}", file=sys.stderr)
                return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
