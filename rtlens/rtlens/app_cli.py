from __future__ import annotations

import argparse
import os
import shlex
import sys
from typing import List, Tuple

from .app import SvViewApp
from .callable_resolver import explain_callable_resolution
from .slang_backend import SlangBackendError, load_design_with_slang
from .sv_parser import discover_sv_files, read_filelist_with_args


def _default_editor_cmd_template() -> str:
    if os.name == "nt":
        return "notepad {fileq}"
    if sys.platform == "darwin":
        return "open -a TextEdit {fileq}"
    return "xdg-open {fileq}"


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the shared CLI parser used by both Tk and Qt entry points."""
    p = argparse.ArgumentParser(
        description="RTLens: simple SV source + hierarchy + connectivity + wave viewer"
    )
    p.add_argument(
        "--ui",
        default="qt",
        choices=["tk", "qt"],
        help="GUI backend: tk|qt (default: qt; tk is legacy)",
    )
    p.add_argument("--filelist", action="append", default=[], help="simulator style filelist (repeatable)")
    p.add_argument("--rtl-file", action="append", default=[], help="RTL file path (repeatable)")
    p.add_argument("--dir", default="", help="RTL root directory")
    p.add_argument("--top", default="", help="top module name")
    p.add_argument("--timescale", default="", help="override default timescale passed to slang (e.g. 1ns/1ps)")
    p.add_argument(
        "--slang-arg",
        action="append",
        default=[],
        help="extra argument token forwarded to slang (repeatable)",
    )
    p.add_argument(
        "--slang-opts",
        default="",
        help="extra slang args as one shell-like string (split by shlex)",
    )
    p.add_argument("--wave", default="", help="wave file (.vcd or .fst)")
    p.add_argument(
        "--wave-viewer",
        default="auto",
        help="external wave viewer bridge (supported: auto|surfer|gtkwave|off)",
    )
    p.add_argument(
        "--surfer-cmd",
        default="surfer",
        help="surfer executable name/path used by the external wave bridge",
    )
    p.add_argument(
        "--gtkwave-cmd",
        default="gtkwave",
        help="gtkwave executable name/path used by the external wave bridge",
    )
    p.add_argument(
        "--wave-import-primary",
        action="store_true",
        help="also try PRIMARY selection for Import Wave Sel (default: CLIPBOARD only)",
    )
    p.add_argument(
        "--editor-cmd",
        default=None,
        help=(
            "external editor argv template (no shell; supports non-vim editors), "
            "use {file}/{fileq}/{basename}/{dir} and {line}"
        ),
    )
    p.add_argument(
        "--yosys-cmd",
        default="yosys",
        help="yosys executable name/path used for schematic generation",
    )
    p.add_argument(
        "--netlistsvg-cmd",
        default="netlistsvg",
        help="netlistsvg executable name/path used for schematic generation",
    )
    p.add_argument(
        "--netlistsvg-dir",
        default="",
        help="patched netlistsvg repo dir; if set, use <dir>/bin/netlistsvg.js via node",
    )
    p.add_argument(
        "--sv2v-cmd",
        default="",
        help="optional sv2v executable path used to preconvert SystemVerilog before yosys",
    )
    p.add_argument(
        "--schematic-view",
        default="svg",
        choices=["external", "svg", "webengine"],
        help="how to display generated schematics in Qt: external browser or embedded webengine",
    )
    p.add_argument(
        "--schematic-timeout",
        type=int,
        default=8,
        help="timeout in seconds for schematic generation subtools (sv2v / yosys / netlistsvg)",
    )
    p.add_argument(
        "--rtl-structure-dot-cmd",
        default="dot",
        help="graphviz dot executable name/path used for RTL Structure rendering",
    )
    p.add_argument(
        "--rtl-structure-timeout",
        type=int,
        default=240,
        help="timeout in seconds for RTL Structure Graphviz rendering",
    )
    p.add_argument(
        "--rtl-structure-benchmark-timeout",
        type=int,
        default=2400,
        help="timeout in seconds for RTL Structure ELK benchmark variants; use 0 to wait without timeout",
    )
    p.add_argument(
        "--rtl-structure-mode",
        default="auto",
        choices=["auto", "detailed"],
        help="default RTL Structure ELK mode: auto or detailed",
    )
    p.add_argument(
        "--dev-ui",
        action="store_true",
        help="enable developer UI controls (for example RTL benchmark button)",
    )
    p.add_argument(
        "--schematic-prebuild-top",
        default="",
        help="if set, enable Schematic tab and prebuild schematic cache under this top hierarchy/module",
    )
    p.add_argument(
        "--schematic-prebuild-log-level",
        default="phase",
        choices=["off", "phase", "detail"],
        help="startup stdout log level for schematic prebuild progress",
    )
    p.add_argument(
        "--schematic-prebuild-batch-probe-sec",
        type=int,
        default=20,
        help="batch probe timeout in seconds before falling back to per-module generation; 0 disables probe mode",
    )
    p.add_argument(
        "--schematic-prebuild-fail-ttl-sec",
        type=int,
        default=300,
        help="retry TTL in seconds for previously failed schematic prebuild modules; 0 retries every startup",
    )
    p.add_argument(
        "--debug-callable",
        action="store_true",
        help="run callable definition/reference debug and exit (no GUI)",
    )
    p.add_argument(
        "--debug-callable-site",
        action="append",
        default=[],
        help="site query as FILE:LINE:TOKEN (repeatable)",
    )
    p.add_argument(
        "--debug-callable-key",
        action="append",
        default=[],
        help="callable key query (repeatable), e.g. function:pkg.foo",
    )
    return p


def _extra_slang_args_from_args(args: argparse.Namespace) -> List[str]:
    """Collect extra slang options from normalized CLI arguments."""
    out: List[str] = []
    if getattr(args, "timescale", ""):
        out.extend(["--timescale", args.timescale])
    if getattr(args, "slang_arg", None):
        out.extend(list(args.slang_arg))
    if getattr(args, "slang_opts", ""):
        out.extend(shlex.split(args.slang_opts))
    return out


def _arg_filelists_from_args(args: argparse.Namespace) -> List[str]:
    """Return normalized absolute filelist paths from parsed arguments."""
    raw = getattr(args, "filelist", [])
    if isinstance(raw, str):
        vals = [raw] if raw else []
    else:
        vals = [x for x in raw if x]
    return [os.path.abspath(x) for x in vals]


def _read_multiple_filelists(paths: List[str]) -> Tuple[List[str], List[str]]:
    """Read and merge multiple simulator-style filelists with deduplicated files."""
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


def _collect_rtl_inputs_from_args(args: argparse.Namespace) -> Tuple[List[str], List[str], List[str]]:
    """Resolve all RTL input files and slang args from CLI sources.

    Returns:
        Tuple of ``(rtl_files, slang_args, used_filelists)``.
    """
    files: List[str] = []
    slang_args: List[str] = []
    filelists_used: List[str] = []
    seen = set()

    def _add_file(path: str) -> None:
        if not path:
            return
        ap = os.path.abspath(path)
        if not os.path.isfile(ap):
            return
        if ap in seen:
            return
        seen.add(ap)
        files.append(ap)

    for fl in _arg_filelists_from_args(args):
        if not os.path.isfile(fl):
            continue
        filelists_used.append(fl)
    if filelists_used:
        fl_files, fl_args = _read_multiple_filelists(filelists_used)
        for f in fl_files:
            _add_file(f)
        slang_args.extend(fl_args)

    rtl_raw = getattr(args, "rtl_file", [])
    rtl_vals = [rtl_raw] if isinstance(rtl_raw, str) else list(rtl_raw or [])
    for p in rtl_vals:
        _add_file(p)

    rtl_dir = getattr(args, "dir", "")
    if rtl_dir and os.path.isdir(rtl_dir):
        for f in discover_sv_files(rtl_dir):
            _add_file(f)

    slang_args.extend(_extra_slang_args_from_args(args))
    if not files:
        raise RuntimeError("no RTL inputs found: use --filelist, --rtl-file, or --dir")
    return files, slang_args, filelists_used


def _parse_debug_site(spec: str) -> Tuple[str, int, str]:
    """Parse one ``--debug-callable-site`` token in FILE:LINE:TOKEN format."""
    raw = (spec or "").strip()
    if not raw:
        raise RuntimeError("empty --debug-callable-site")
    try:
        file_s, line_s, token = raw.rsplit(":", 2)
        line = int(line_s)
    except Exception as e:
        raise RuntimeError(f"invalid --debug-callable-site '{raw}', expected FILE:LINE:TOKEN") from e
    if line <= 0:
        raise RuntimeError(f"invalid site line number: {line}")
    return os.path.abspath(file_s), line, token


def run_callable_debug_cli(args: argparse.Namespace) -> int:
    """Run non-GUI callable-resolution diagnostics and print summaries."""
    files, slang_args, filelists_used = _collect_rtl_inputs_from_args(args)
    print("[rtlens] callable debug")
    print(f"top: {getattr(args, 'top', '') or '(auto)'}")
    print(f"rtl files: {len(files)}")
    print(f"filelists: {len(filelists_used)}")
    print(f"slang args: {len(slang_args)}")

    try:
        design, _conn, compile_log = load_design_with_slang(files, getattr(args, "top", ""), slang_args)
    except SlangBackendError as e:
        print(f"error: slang backend load failed: {e}")
        return 2

    callable_keys = sorted(design.callable_defs.keys())
    ref_total = sum(len(v) for v in design.callable_refs.values())
    print(
        "summary: "
        f"modules={len(design.modules)} hier={len(design.hier)} "
        f"callable_defs={len(callable_keys)} callable_ref_keys={len(design.callable_refs)} callable_refs={ref_total}"
    )
    if compile_log:
        print(f"compile_log: {len(compile_log.splitlines())} lines")

    if getattr(args, "debug_callable", False):
        print("\n[callable definitions]")
        max_rows = 200
        for idx, key in enumerate(callable_keys):
            if idx >= max_rows:
                print(f"... truncated ({len(callable_keys) - max_rows} more)")
                break
            loc = design.callable_defs.get(key)
            kind = design.callable_kinds.get(key, "")
            name = design.callable_names.get(key, "")
            refs = len(design.callable_refs.get(key, []))
            file = loc.file if loc else "?"
            line = int(loc.line) if loc else 0
            print(f"- {key} kind={kind} name={name} def={file}:{line} refs={refs}")

    for key in getattr(args, "debug_callable_key", []) or []:
        k = (key or "").strip()
        if not k:
            continue
        print(f"\n[callable key] {k}")
        loc = design.callable_defs.get(k)
        if not loc:
            print("  not found")
            continue
        print(f"  kind={design.callable_kinds.get(k, '')} name={design.callable_names.get(k, '')}")
        print(f"  def={loc.file}:{int(loc.line)}")
        refs = design.callable_refs.get(k, [])
        print(f"  refs={len(refs)}")
        for r in refs[:40]:
            print(f"    - {r.file}:{int(r.line)}")
        if len(refs) > 40:
            print(f"    ... truncated ({len(refs) - 40} more)")

    for spec in getattr(args, "debug_callable_site", []) or []:
        file, line, token = _parse_debug_site(spec)
        print(f"\n[site] {file}:{line}:{token}")
        info = explain_callable_resolution(design, file, line, token, current_hier_path="")
        print(f"  tokens={info.get('site', {}).get('tokens', [])}")
        print(f"  resolved_from_site={info.get('resolved_from_site')}")
        print(f"  resolved_for_definition_site={info.get('resolved_for_definition_site')}")
        cands = list(info.get("candidates", []))
        print(f"  candidates={len(cands)}")
        for c in cands[:40]:
            print(
                "    - "
                f"{c.get('key')} kind={c.get('kind')} name={c.get('name')} "
                f"hits={c.get('site_hits')} def={c.get('def_file')}:{c.get('def_line')}"
            )
        if len(cands) > 40:
            print(f"    ... truncated ({len(cands) - 40} more)")

    return 0


def main() -> None:
    """CLI entry point for rtlens.

    - Runs callable debug mode when debug flags are given.
    - Starts Qt GUI by default (or when ``--ui qt`` is selected).
    - Starts Tk GUI only when ``--ui tk`` is explicitly selected (deprecated path).
    """
    args = build_arg_parser().parse_args()
    if args.debug_callable or args.debug_callable_site or args.debug_callable_key:
        raise SystemExit(run_callable_debug_cli(args))
    try:
        if getattr(args, "ui", "qt") == "qt":
            from .qt_app import run_qt

            run_qt(args)
            return
        if getattr(args, "editor_cmd", None) is None:
            args.editor_cmd = _default_editor_cmd_template()
        app = SvViewApp(args)
        app.run()
    except RuntimeError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
