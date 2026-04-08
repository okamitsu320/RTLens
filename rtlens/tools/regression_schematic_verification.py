#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO / "rtlens"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from rtlens.netlistsvg_view import generate_netlistsvg_prebuild_batch
from rtlens.sv_parser import read_filelist_with_args


@dataclass(frozen=True)
class CaseSpec:
    name: str
    filelist: Path
    top: str
    modules: List[str]
    min_top_data_src: int


CASES: Dict[str, CaseSpec] = {
    "min_case": CaseSpec(
        name="min_case",
        filelist=REPO / "RTL" / "verification" / "min_case" / "vlist",
        top="vm_min_top",
        modules=["vm_min_top", "vm_min_stage", "vm_min_leaf"],
        min_top_data_src=1,
    ),
    "mid_case": CaseSpec(
        name="mid_case",
        filelist=REPO / "RTL" / "verification" / "mid_case" / "vlist",
        top="vm_mid_top",
        modules=["vm_mid_top"],
        min_top_data_src=1,
    ),
    "deep_case": CaseSpec(
        name="deep_case",
        filelist=REPO / "RTL" / "verification" / "deep_case" / "vlist",
        top="vm_deep_top",
        modules=["vm_deep_top", "vm_deep_cluster"],
        min_top_data_src=1,
    ),
}


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _tool_is_available(netlistsvg_dir: str, netlistsvg_cmd: str, yosys_cmd: str) -> tuple[bool, str]:
    if shutil.which(yosys_cmd) is None:
        return False, f"missing yosys tool: {yosys_cmd}"
    if netlistsvg_dir:
        script = Path(netlistsvg_dir) / "bin" / "netlistsvg.js"
        if not script.is_file():
            return False, f"netlistsvg_dir does not contain bin/netlistsvg.js: {script}"
        if shutil.which("node") is None:
            return False, "missing node required for netlistsvg.js"
        return True, ""
    if shutil.which(netlistsvg_cmd) is None:
        return False, f"missing netlistsvg tool: {netlistsvg_cmd}"
    return True, ""


def _extract_data_src_values(svg_text: str) -> List[str]:
    rx = re.compile(r'\bdata-src="([^"]+)"')
    return [m.group(1).strip() for m in rx.finditer(svg_text) if m.group(1).strip()]


def _count_resolved_data_src(values: List[str]) -> int:
    hits = 0
    for raw in values:
        text = raw.strip()
        if not text:
            continue
        file_part = ""
        line_part = ""
        if ":" in text:
            file_part, line_part = text.rsplit(":", 1)
        if not file_part:
            continue
        if line_part and not line_part.isdigit():
            continue
        if os.path.isfile(file_part):
            hits += 1
    return hits


def _run_one_case(args: argparse.Namespace, case: CaseSpec) -> int:
    if not case.filelist.is_file():
        return _fail(f"{case.name}: missing filelist {case.filelist}")

    files, extra_args = read_filelist_with_args(str(case.filelist))
    if not files:
        return _fail(f"{case.name}: filelist has no RTL files")
    missing_files = [f for f in files if not os.path.isfile(f)]
    if missing_files:
        return _fail(f"{case.name}: filelist resolved missing RTL file: {missing_files[0]}")

    results = generate_netlistsvg_prebuild_batch(
        files=files,
        top_module=case.top,
        module_names=case.modules,
        extra_args=extra_args,
        yosys_cmd=args.yosys_cmd,
        netlistsvg_cmd=args.netlistsvg_cmd,
        netlistsvg_dir=args.netlistsvg_dir,
        sv2v_cmd=args.sv2v_cmd,
        timeout_sec=args.timeout_sec,
    )

    top_data_src_count = 0
    top_resolved_count = 0
    for mod in case.modules:
        res = results.get(mod)
        if res is None:
            return _fail(f"{case.name}: missing result entry for module {mod}")
        if res.error:
            if args.detail:
                print(f"[DETAIL] {case.name}:{mod}: {res.error}")
                if res.log:
                    print(res.log)
            return _fail(f"{case.name}: module {mod} failed: {res.error}")
        for kind, path in (("html", res.html_path), ("svg", res.svg_path), ("json", res.json_path)):
            if not path or not os.path.isfile(path):
                return _fail(f"{case.name}: module {mod} missing {kind} output")
        try:
            svg_text = Path(res.svg_path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return _fail(f"{case.name}: module {mod} failed to read svg: {e}")
        data_src_values = _extract_data_src_values(svg_text)
        if mod == case.top:
            top_data_src_count = len(data_src_values)
            top_resolved_count = _count_resolved_data_src(data_src_values)

    if top_data_src_count < case.min_top_data_src:
        return _fail(
            f"{case.name}: top module {case.top} has too few data-src entries "
            f"({top_data_src_count} < {case.min_top_data_src})"
        )
    if top_resolved_count <= 0:
        return _fail(f"{case.name}: top module {case.top} has no resolvable data-src entries")

    _pass(
        f"{case.name}: generated modules={len(case.modules)} "
        f"top_data_src={top_data_src_count} top_resolved_data_src={top_resolved_count}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regression for verification RTL schematic prebuild outputs")
    p.add_argument(
        "--case",
        action="append",
        default=[],
        choices=["min_case", "mid_case", "deep_case", "all"],
        help="target case name (repeatable). default: all",
    )
    p.add_argument("--timeout-sec", type=int, default=90, help="per-tool timeout for schematic generation")
    p.add_argument("--yosys-cmd", default="yosys", help="yosys executable name/path")
    p.add_argument("--netlistsvg-cmd", default="netlistsvg", help="netlistsvg executable name/path")
    p.add_argument("--netlistsvg-dir", default="", help="optional patched netlistsvg repo directory")
    p.add_argument("--sv2v-cmd", default="", help="optional sv2v executable name/path")
    p.add_argument("--detail", action="store_true", help="print detailed logs on failures")
    p.add_argument(
        "--strict-tools",
        action="store_true",
        help="fail instead of skip when yosys/netlistsvg tools are unavailable",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    ok, reason = _tool_is_available(args.netlistsvg_dir, args.netlistsvg_cmd, args.yosys_cmd)
    if not ok:
        if args.strict_tools:
            return _fail(reason)
        _pass(f"skip: {reason}")
        return 0

    selected = args.case or ["all"]
    run_names: List[str] = []
    if "all" in selected:
        run_names = sorted(CASES.keys())
    else:
        seen = set()
        for name in selected:
            if name in seen:
                continue
            seen.add(name)
            run_names.append(name)

    all_ok = True
    for name in run_names:
        rc = _run_one_case(args, CASES[name])
        if rc != 0:
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
