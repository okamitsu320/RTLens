from __future__ import annotations

import argparse
import os
import shlex
from typing import List, Optional, Set

from .connectivity import build_connectivity, build_hierarchy
from .model import DesignDB
from .rtl_debug import run_rtl_debug_pipeline
from .slang_backend import SlangBackendError, load_design_with_slang
from .sv_parser import discover_sv_files, parse_sv_files, read_filelist_with_args


def _extra_slang_args(args: argparse.Namespace) -> List[str]:
    out: List[str] = []
    if getattr(args, "timescale", ""):
        out.extend(["--timescale", args.timescale])
    if getattr(args, "slang_arg", None):
        out.extend(args.slang_arg)
    if getattr(args, "slang_opts", ""):
        out.extend(shlex.split(args.slang_opts))
    return out


def _defined_macros_from_args(arg_tokens: List[str]) -> Set[str]:
    out: Set[str] = set()
    i = 0
    while i < len(arg_tokens):
        tok = arg_tokens[i]
        if tok.startswith("+define+"):
            for item in tok.split("+")[2:]:
                if item:
                    out.add(item.split("=", 1)[0])
        elif tok == "-D" and i + 1 < len(arg_tokens):
            val = arg_tokens[i + 1]
            if val:
                out.add(val.split("=", 1)[0])
            i += 1
        elif tok.startswith("-D") and len(tok) > 2:
            out.add(tok[2:].split("=", 1)[0])
        i += 1
    return out


def _should_update_line_range(
    existing_start: int,
    existing_end: int,
    parsed_start: int,
    parsed_end: int,
) -> bool:
    if parsed_start <= 0 or parsed_end < parsed_start:
        return False
    if existing_start <= 0 or existing_end < existing_start:
        return True
    existing_span = existing_end - existing_start + 1
    parsed_span = parsed_end - parsed_start + 1
    # slang can report module range as declaration-only (for example 1/1),
    # which drops callable extraction that depends on real line coverage.
    if existing_span <= 1 and parsed_span > existing_span:
        return True
    return False


def _merge_parser_structure_into_design(
    design: DesignDB,
    files: List[str],
    defined_macros: Set[str],
) -> dict[str, int]:
    parsed = parse_sv_files(files, defined_macros=defined_macros)
    merged = 0
    added = 0
    line_range_patched = 0
    for mod_name, parsed_mod in parsed.modules.items():
        existing = design.modules.get(mod_name)
        if existing is None:
            design.modules[mod_name] = parsed_mod
            added += 1
            continue
        if not existing.file and parsed_mod.file:
            existing.file = parsed_mod.file
        if _should_update_line_range(
            existing_start=existing.start_line,
            existing_end=existing.end_line,
            parsed_start=parsed_mod.start_line,
            parsed_end=parsed_mod.end_line,
        ):
            existing.start_line = parsed_mod.start_line
            existing.end_line = parsed_mod.end_line
            line_range_patched += 1
        if not existing.ports and parsed_mod.ports:
            existing.ports = dict(parsed_mod.ports)
        if not existing.signals and parsed_mod.signals:
            existing.signals = dict(parsed_mod.signals)
        if not existing.instances and parsed_mod.instances:
            existing.instances = list(parsed_mod.instances)
        if not existing.assignments and parsed_mod.assignments:
            existing.assignments = list(parsed_mod.assignments)
        if not getattr(existing, "always_blocks", None) and getattr(parsed_mod, "always_blocks", None):
            existing.always_blocks = list(parsed_mod.always_blocks)
        merged += 1
    return {
        "merged_modules": merged,
        "added_modules": added,
        "parsed_modules": len(parsed.modules),
        "line_range_patched": line_range_patched,
    }


def _read_multiple_filelists(paths: List[str]) -> tuple[List[str], List[str]]:
    all_files: List[str] = []
    all_args: List[str] = []
    seen_files = set()
    for path in paths:
        files, slang_args = read_filelist_with_args(path)
        for f in files:
            af = os.path.abspath(f)
            if af in seen_files:
                continue
            seen_files.add(af)
            all_files.append(af)
        all_args.extend(slang_args)
    return all_files, all_args


def _load_design(files: List[str], top: str, slang_args: List[str]) -> tuple[DesignDB, str]:
    try:
        design, _connectivity, compile_log = load_design_with_slang(files, top, slang_args)
        stats = _merge_parser_structure_into_design(design, files, _defined_macros_from_args(slang_args))
        compile_log += (
            "\n\n[rtlens] rtl structure merge\n"
            f"merged_modules={stats['merged_modules']} "
            f"added_modules={stats['added_modules']} "
            f"parsed_modules={stats['parsed_modules']} "
            f"line_range_patched={stats['line_range_patched']}"
        )
        return design, compile_log
    except SlangBackendError:
        design = parse_sv_files(files, defined_macros=_defined_macros_from_args(slang_args))
        build_hierarchy(design, top)
        _ = build_connectivity(design)
        return design, "[rtlens] debug_rtl fallback parser used"


def _collect_files(args: argparse.Namespace) -> tuple[List[str], List[str]]:
    filelists = [os.path.abspath(x) for x in (args.filelist or []) if x]
    if filelists:
        files, fl_args = _read_multiple_filelists(filelists)
        return files, fl_args
    if args.dir:
        return discover_sv_files(args.dir), []
    return [], []


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RTLens RTL debug dump (no GUI)")
    p.add_argument("--filelist", action="append", default=[], help="simulator style filelist (repeatable)")
    p.add_argument("--dir", default="", help="RTL root directory")
    p.add_argument("--top", default="", help="top module name")
    p.add_argument("--hier", required=True, help="hierarchy path (example: tb_top_pipeline.U_DUT)")
    p.add_argument("--module", default="", help="expected module name for sanity check")
    p.add_argument("--mode", default="auto", choices=["auto", "detailed"], help="ELK mode")
    p.add_argument("--node-cmd", default="node", help="node executable for ELK runner")
    p.add_argument("--timeout", type=int, default=240, help="ELK timeout seconds (0 = no timeout)")
    p.add_argument("--debug-dir", default="", help="debug dump root directory")
    p.add_argument("--timescale", default="", help="override default timescale passed to slang")
    p.add_argument("--slang-arg", action="append", default=[], help="extra argument token forwarded to slang")
    p.add_argument("--slang-opts", default="", help="extra slang args as one shell-like string")
    p.add_argument("--no-layout", action="store_true", help="skip ELK layout execution and dump only pre-layout stages")
    p.add_argument("--print-summary", action="store_true", help="print summary.log content to stdout")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    files, fl_args = _collect_files(args)
    if not files:
        raise SystemExit("no input files: use --filelist or --dir")

    merged_args = list(fl_args) + _extra_slang_args(args)
    design, compile_log = _load_design(files, args.top, merged_args)
    if args.hier not in design.hier:
        raise SystemExit(f"hier path not found: {args.hier}")
    module_name = design.hier[args.hier].module_name
    if args.module and args.module != module_name:
        raise SystemExit(f"module mismatch: expected={args.module} actual={module_name}")

    timeout: Optional[int] = args.timeout if args.timeout > 0 else None
    dump = run_rtl_debug_pipeline(
        design=design,
        hier_path=args.hier,
        mode=args.mode,
        node_cmd=args.node_cmd,
        timeout=timeout,
        debug_root=(args.debug_dir or None),
        run_layout=not args.no_layout,
    )

    print("[rtlens] rtl debug dump")
    print(f"module: {module_name}")
    print(f"hier_path: {args.hier}")
    print(f"input files: {len(files)}")
    print(f"slang args: {len(merged_args)}")
    print(f"compile log lines: {len(compile_log.splitlines())}")
    print(f"run dir: {dump['run_dir']}")
    print(f"summary: {dump['summary_path']}")
    if dump["layout_error"]:
        print(f"layout: error: {dump['layout_error']}")
    elif dump["layout"] is None:
        print("layout: skipped")
    else:
        width = float(dump["layout"].get("width", 0.0))
        height = float(dump["layout"].get("height", 0.0))
        print(f"layout: ok ({width:.1f} x {height:.1f})")
    if args.print_summary:
        print("\n--- summary.log ---")
        print(dump["summary_text"], end="")


if __name__ == "__main__":
    main()
