from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from .sv_parser import parse_sv_files
from .yosys_runner import (
    _emit_metric_event,
    _emit_progress_event,
    _run_command_with_heartbeat,
    _run_netlistsvg_from_json,
)
from .netlistsvg_svg import (
    _build_html,
    _canonical_module_name,
    _find_json_module_key,
    _inject_svg_data_src_from_json,
    _inline_svg_styles_for_qt,
)


SV_PRIMITIVE_WORDS = {
    "begin",
    "end",
    "if",
    "else",
    "for",
    "while",
    "case",
    "endcase",
    "always",
    "always_ff",
    "always_comb",
    "always_latch",
    "initial",
    "assign",
}

SV_RESERVED_WORDS = {
    "always",
    "always_comb",
    "always_ff",
    "always_latch",
    "assign",
    "begin",
    "case",
    "default",
    "else",
    "end",
    "endcase",
    "endgenerate",
    "endmodule",
    "for",
    "function",
    "generate",
    "genvar",
    "if",
    "initial",
    "localparam",
    "module",
    "parameter",
    "task",
    "while",
}
SV_RESERVED_WORDS.update(SV_PRIMITIVE_WORDS)

_SV_IDENTIFIER_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass
class NetlistSvgResult:
    """Output bundle for one netlistsvg generation target."""

    module_name: str
    html_path: str = ""
    svg_path: str = ""
    json_path: str = ""
    log: str = ""
    error: str = ""


def canonical_file_key(path: str) -> str:
    """Build a canonical path key for case-insensitive deduplication."""
    ap = os.path.abspath(path)
    rp = os.path.realpath(ap)
    return os.path.normcase(os.path.normpath(rp))


def _normalize_module_symbol(name: str) -> str:
    return str(name or "").strip().lstrip("\\")


def _is_valid_blackbox_module_symbol(name: str) -> bool:
    symbol = _normalize_module_symbol(name)
    if not symbol:
        return False
    if not _SV_IDENTIFIER_RX.match(symbol):
        return False
    if symbol.lower() in SV_RESERVED_WORDS:
        return False
    return True


def dedupe_existing_files_canonical(files: Iterable[str]) -> tuple[List[str], Dict[str, int]]:
    """Drop non-existing and duplicate file paths using canonical normalization."""
    out: List[str] = []
    seen: Set[str] = set()
    input_files = 0
    dropped_files = 0
    for raw in files:
        if not raw:
            continue
        ap = os.path.abspath(str(raw))
        if not os.path.isfile(ap):
            continue
        input_files += 1
        key = canonical_file_key(ap)
        if key in seen:
            dropped_files += 1
            continue
        seen.add(key)
        out.append(ap)
    return out, {
        "input_files": input_files,
        "unique_files": len(out),
        "dropped_files": dropped_files,
    }


def _normalize_stmt(text: str) -> str:
    text = re.sub(r"//.*$", "", text, flags=re.M)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"\s+", "", text)
    return text


def _collect_statement_map(files: Iterable[str]) -> Dict[str, List[tuple[str, int]]]:
    out: Dict[str, List[tuple[str, int]]] = {}
    for path in files:
        ap = os.path.abspath(path)
        try:
            lines = Path(ap).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        acc: List[str] = []
        start_line = 1
        in_block = False
        for idx, raw in enumerate(lines, 1):
            line = raw
            if in_block:
                end = line.find("*/")
                if end < 0:
                    continue
                in_block = False
                line = line[end + 2 :]
            while "/*" in line:
                a = line.find("/*")
                b = line.find("*/", a + 2)
                if b < 0:
                    in_block = True
                    line = line[:a]
                    break
                line = line[:a] + " " + line[b + 2 :]
            line = re.sub(r"//.*$", "", line).rstrip()
            if not line.strip():
                continue
            if not acc:
                start_line = idx
            acc.append(line.strip())
            joined = " ".join(acc)
            end_stmt = False
            stripped = joined.strip()
            if stripped.startswith("module ") and ");" in stripped:
                end_stmt = True
            elif stripped.startswith("endmodule"):
                end_stmt = True
            elif ";" in stripped:
                end_stmt = True
            if end_stmt:
                norm = _normalize_stmt(joined)
                if norm:
                    out.setdefault(norm, []).append((ap, start_line))
                acc = []
        if acc:
            norm = _normalize_stmt(" ".join(acc))
            if norm:
                out.setdefault(norm, []).append((ap, start_line))
    return out


def _build_sv2v_line_map(sv2v_file: str, original_files: Iterable[str]) -> Dict[int, tuple[str, int]]:
    original_map = _collect_statement_map(original_files)
    line_map: Dict[int, tuple[str, int]] = {}
    original_lines: List[tuple[str, int, str]] = []
    for path in original_files:
        ap = os.path.abspath(path)
        try:
            for idx, line in enumerate(Path(ap).read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                original_lines.append((ap, idx, line))
        except Exception:
            continue

    def fallback_loc(stmt: str) -> Optional[tuple[str, int]]:
        patterns = [
            re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)"),
            re.compile(r"^\s*(?:wire|logic|reg|localparam|parameter)\b.*?\b([A-Za-z_][A-Za-z0-9_$]*)\b\s*(?:=|;|\[)"),
            re.compile(r"^\s*assign\s+([A-Za-z_][A-Za-z0-9_$]*)\b"),
        ]
        token = ""
        head = ""
        for rx in patterns:
            m = rx.match(stmt)
            if m:
                token = m.group(1)
                head = stmt.strip().split(None, 1)[0]
                break
        if not token:
            return None
        hits: List[tuple[str, int]] = []
        for ap, idx, raw in original_lines:
            s = raw.strip()
            if not s or token not in s:
                continue
            if head and not s.startswith(head):
                continue
            hits.append((ap, idx))
        return hits[0] if len(hits) == 1 else None

    try:
        lines = Path(sv2v_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return line_map
    acc: List[str] = []
    start_line = 1
    for idx, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        if not acc:
            start_line = idx
        acc.append(raw.strip())
        joined = " ".join(acc)
        stripped = joined.strip()
        end_stmt = False
        if stripped.startswith("module ") and ");" in stripped:
            end_stmt = True
        elif stripped.startswith("endmodule"):
            end_stmt = True
        elif ";" in stripped:
            end_stmt = True
        if end_stmt:
            norm = _normalize_stmt(joined)
            cands = original_map.get(norm, [])
            if len(cands) == 1:
                line_map[start_line] = cands[0]
            else:
                loc = fallback_loc(joined)
                if loc:
                    line_map[start_line] = loc
            acc = []
    return line_map


def _remap_src_string(src: str, line_map: Dict[int, tuple[str, int]]) -> str:
    if not src or not line_map:
        return src
    parts: List[str] = []
    for chunk in src.split("|"):
        chunk = chunk.strip()
        m = re.match(r"^(.*?):(\d+)(.*)$", chunk)
        if not m:
            parts.append(chunk)
            continue
        try:
            line = int(m.group(2))
        except ValueError:
            parts.append(chunk)
            continue
        repl = line_map.get(line)
        if not repl:
            parts.append(chunk)
            continue
        parts.append(f"{repl[0]}:{repl[1]}")
    return "|".join(parts)


def _augment_yosys_json(json_path: Path, parsed_db, module_name: str, line_map: Dict[int, tuple[str, int]]) -> None:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return
    modules = data.get("modules", {})
    for mod_name, mod in modules.items():
        attrs = mod.setdefault("attributes", {})
        if "src" in attrs:
            attrs["src"] = _remap_src_string(str(attrs["src"]), line_map)
        parsed_mod = parsed_db.modules.get(mod_name) if parsed_db else None
        ports = mod.get("ports", {}) or {}
        for pname, port in ports.items():
            pattrs = port.setdefault("attributes", {})
            if "src" in pattrs:
                pattrs["src"] = _remap_src_string(str(pattrs["src"]), line_map)
            elif parsed_mod:
                loc = parsed_mod.ports.get(pname) or parsed_mod.signals.get(pname)
                if loc:
                    pattrs["src"] = f"{os.path.abspath(parsed_mod.file)}:{loc.line}"
        netnames = mod.get("netnames", {}) or mod.get("netNames", {}) or {}
        for nname, net in netnames.items():
            nattrs = net.setdefault("attributes", {})
            if "src" in nattrs:
                nattrs["src"] = _remap_src_string(str(nattrs["src"]), line_map)
            elif parsed_mod:
                loc = parsed_mod.signals.get(nname) or parsed_mod.ports.get(nname)
                if loc:
                    nattrs["src"] = f"{os.path.abspath(parsed_mod.file)}:{loc.line}"
        for _cname, cell in (mod.get("cells", {}) or {}).items():
            cattrs = cell.setdefault("attributes", {})
            if "src" in cattrs:
                cattrs["src"] = _remap_src_string(str(cattrs["src"]), line_map)
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _translate_slang_args_to_yosys(args: Iterable[str]) -> List[str]:
    def _norm_path(p: str) -> str:
        s = str(p or "")
        if os.name == "nt":
            return s.replace("\\", "/")
        return s

    out: List[str] = []
    items = list(args)
    i = 0
    while i < len(items):
        tok = items[i]
        if tok.startswith("+incdir+"):
            for p in tok.split("+")[2:]:
                if p:
                    out.append(f"-I{_norm_path(p)}")
        elif tok.startswith("+define+"):
            for d in tok.split("+")[2:]:
                if d:
                    out.append(f"-D{d}")
        elif tok == "-D" and i + 1 < len(items):
            out.extend(["-D", items[i + 1]])
            i += 1
        elif tok.startswith("-D") and len(tok) > 2:
            out.append(tok)
        elif tok == "-I" and i + 1 < len(items):
            out.extend(["-I", _norm_path(items[i + 1])])
            i += 1
        elif tok.startswith("-I") and len(tok) > 2:
            out.append("-I" + _norm_path(tok[2:]))
        i += 1
    return out


def _translate_slang_args_to_sv2v(args: Iterable[str]) -> List[str]:
    def _norm_path(p: str) -> str:
        s = str(p or "")
        if os.name == "nt":
            return s.replace("\\", "/")
        return s

    out: List[str] = []
    items = list(args)
    i = 0
    while i < len(items):
        tok = items[i]
        if tok.startswith("+incdir+"):
            for p in tok.split("+")[2:]:
                if p:
                    out.extend(["-I", _norm_path(p)])
        elif tok.startswith("+define+"):
            for d in tok.split("+")[2:]:
                if d:
                    out.extend(["-D", d])
        elif tok == "-D" and i + 1 < len(items):
            out.extend(["-D", items[i + 1]])
            i += 1
        elif tok.startswith("-D") and len(tok) > 2:
            out.append(tok)
        elif tok == "-I" and i + 1 < len(items):
            out.extend(["-I", _norm_path(items[i + 1])])
            i += 1
        elif tok.startswith("-I") and len(tok) > 2:
            out.extend(["-I", _norm_path(tok[2:])])
        i += 1
    return out


def _normalize_tool_path(path: str) -> str:
    text = str(path or "")
    if os.name == "nt":
        return text.replace("\\", "/")
    return text


def _yosys_quote_arg(arg: str) -> str:
    text = str(arg or "")
    if not text:
        return '""'
    if not any(ch in text for ch in (' ', '\t', '\n', '\r', ';', '"', "'")):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _extract_defined_macros(args: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    items = list(args)
    i = 0
    while i < len(items):
        tok = str(items[i] or "")
        if tok.startswith("+define+"):
            for d in tok.split("+")[2:]:
                key = str(d).split("=", 1)[0].strip()
                if key:
                    out.add(key)
        elif tok == "-D" and i + 1 < len(items):
            key = str(items[i + 1] or "").split("=", 1)[0].strip()
            if key:
                out.add(key)
            i += 1
        elif tok.startswith("-D") and len(tok) > 2:
            key = tok[2:].split("=", 1)[0].strip()
            if key:
                out.add(key)
        i += 1
    return out


def _module_closure(parsed_db, top_module: str) -> Set[str]:
    if parsed_db is None or top_module not in parsed_db.modules:
        return {top_module} if top_module else set()
    out: Set[str] = set()
    stack = [top_module]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        mod = parsed_db.modules.get(cur)
        if not mod:
            continue
        for inst in mod.instances:
            if inst.module_type in parsed_db.modules and inst.module_type not in out:
                stack.append(inst.module_type)
    return out


def _select_yosys_input_files(abs_files: List[str], module_name: str, parsed_db) -> List[str]:
    if parsed_db is None or module_name not in parsed_db.modules:
        return abs_files
    needed_mods = _module_closure(parsed_db, module_name)
    selected: List[str] = []
    seen: Set[str] = set()
    for mod_name in sorted(needed_mods):
        mod = parsed_db.modules.get(mod_name)
        if not mod:
            continue
        path = os.path.abspath(mod.file)
        if path not in seen and os.path.isfile(path):
            selected.append(path)
            seen.add(path)
    for path in abs_files:
        if path in seen:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(4096)
        except Exception:
            continue
        if re.search(r"^\s*package\s+[A-Za-z_][A-Za-z0-9_$]*", head, re.M):
            selected.append(path)
            seen.add(path)
    return selected or abs_files


def _select_sv2v_support_files(abs_files: List[str], module_name: str, parsed_db) -> List[str]:
    selected = _select_yosys_input_files(abs_files, module_name, parsed_db)
    seen = set(selected)
    import_rx = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_$]*)::", re.M)
    package_decl_rx = re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_$]*)", re.M)
    imported_pkgs: Set[str] = set()
    for path in selected:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        imported_pkgs.update(import_rx.findall(text))
    if not imported_pkgs:
        return selected or abs_files
    for path in abs_files:
        if path in seen:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        decls = set(package_decl_rx.findall(text))
        if decls & imported_pkgs:
            selected.append(path)
            seen.add(path)
    return selected or abs_files


def _extract_missing_modules(text: str) -> Set[str]:
    out: Set[str] = set()
    rx = re.compile(r"Module `\\\\?([A-Za-z_][A-Za-z0-9_$]*)' referenced")
    for m in rx.finditer(text or ""):
        out.add(m.group(1))
    return out


def _extract_missing_ports(text: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    rx = re.compile(r"Module `\\?([A-Za-z_][A-Za-z0-9_$]*)'.*does not have a port named '\\?([A-Za-z_][A-Za-z0-9_$]*)'")
    for m in rx.finditer(text or ""):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def _extract_missing_parameters(text: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    merged = text or ""
    named_rx = re.compile(r"Module `\\?([A-Za-z_][A-Za-z0-9_$]*)'.*does not have a parameter named '\\?([A-Za-z_][A-Za-z0-9_$]*)'")
    for m in named_rx.finditer(merged):
        out.setdefault(m.group(1), set()).add(m.group(2))
    positional_rx = re.compile(r"Module `\\?([A-Za-z_][A-Za-z0-9_$]*)'.*has only \d+ parameters, requested parameter (\d+)")
    for m in positional_rx.finditer(merged):
        mod = m.group(1)
        try:
            requested = int(m.group(2))
        except Exception:
            continue
        params = out.setdefault(mod, set())
        for idx in range(max(0, requested + 1)):
            params.add(f"p{idx}")
    return out


def _extract_positional_port_requests(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    rx = re.compile(r"Module `\\?([A-Za-z_][A-Za-z0-9_$]*)'.*has only \d+ ports, requested port (\d+)")
    for m in rx.finditer(text or ""):
        mod = m.group(1)
        try:
            requested = int(m.group(2))
        except Exception:
            continue
        needed = max(0, requested + 1)
        prev = int(out.get(mod, 0))
        if needed > prev:
            out[mod] = needed
    return out


def _extract_sv2v_missing_modules(text: str) -> Set[str]:
    merged = str(text or "")
    out: Set[str] = set()
    patterns = [
        re.compile(r"(?:[Uu]nknown|[Uu]ndefined|[Uu]nbound)\s+module\s+[`'\\\"]?([A-Za-z_][A-Za-z0-9_$]*)"),
        re.compile(r"[Mm]odule\s+[`'\\\"]?([A-Za-z_][A-Za-z0-9_$]*)[`'\\\"]?\s+(?:is\s+not\s+defined|not\s+found|is\s+undefined)"),
        re.compile(r"[Cc]annot\s+find\s+(?:module|definition)\s+(?:for\s+)?[`'\\\"]?([A-Za-z_][A-Za-z0-9_$]*)"),
    ]
    for rx in patterns:
        for m in rx.finditer(merged):
            out.add(m.group(1))
    return out


def _expand_sv2v_support_files(
    parsed_db,
    missing_modules: Set[str],
    current_files: List[str],
) -> tuple[List[str], List[str]]:
    if not parsed_db or not missing_modules:
        return [], []
    seen_files = set(current_files)
    seen_mods: Set[str] = set()
    added_files: List[str] = []
    added_mods: List[str] = []
    for raw in sorted(missing_modules):
        mod_name = str(raw or "").strip().lstrip("\\")
        if not mod_name:
            continue
        mod = parsed_db.modules.get(mod_name)
        if not mod:
            continue
        path = os.path.abspath(mod.file)
        if not os.path.isfile(path):
            continue
        if mod_name not in seen_mods:
            seen_mods.add(mod_name)
            added_mods.append(mod_name)
        if path not in seen_files:
            seen_files.add(path)
            added_files.append(path)
    return added_mods, added_files


def _select_sv2v_minimal_files(abs_files: List[str], module_name: str, parsed_db) -> List[str]:
    if parsed_db is None or module_name not in parsed_db.modules:
        return []
    # Use aggressive single-file sv2v seeding for:
    # - leaf modules, or
    # - very large closures (fail-fast mode to avoid long startup stalls).
    #
    # Mid-sized hierarchical modules should keep closure-based support files,
    # otherwise required children are often dropped and yosys reports
    # "Module ... is not part of the design".
    closure = _module_closure(parsed_db, module_name)
    if len(closure) > 1 and len(closure) < 40:
        return []
    module_file = os.path.abspath(parsed_db.modules[module_name].file)
    if not os.path.isfile(module_file):
        return []
    selected = [module_file]
    seen = {module_file}
    import_rx = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_$]*)::", re.M)
    package_decl_rx = re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_$]*)", re.M)
    imported_pkgs: Set[str] = set()
    try:
        module_text = Path(module_file).read_text(encoding="utf-8", errors="ignore")
        imported_pkgs.update(import_rx.findall(module_text))
    except Exception:
        pass
    if not imported_pkgs:
        return selected
    for path in abs_files:
        ap = os.path.abspath(path)
        if ap in seen:
            continue
        try:
            text = Path(ap).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        decls = set(package_decl_rx.findall(text))
        if decls & imported_pkgs:
            selected.append(ap)
            seen.add(ap)
    return selected


def _fill_stub_ports_from_instances(
    parsed_db,
    module_names: Set[str],
    stub_ports: Dict[str, Set[str]],
    stub_params: Dict[str, Set[str]],
) -> None:
    if not parsed_db or not module_names:
        return
    for mod in parsed_db.modules.values():
        for inst in mod.instances:
            if inst.module_type not in module_names:
                continue
            if inst.connections:
                stub_ports.setdefault(inst.module_type, set()).update(inst.connections.keys())
            if inst.positional:
                ports = stub_ports.setdefault(inst.module_type, set())
                for idx in range(len(inst.positional)):
                    ports.add(f"p{idx}")
            if inst.parameters:
                stub_params.setdefault(inst.module_type, set()).update(inst.parameters.keys())


def _guess_port_direction(port_name: str) -> str:
    name = str(port_name or "").strip().lower()
    if not name:
        return "input"
    if name in {"q", "qn", "qb", "y", "z", "result", "out"}:
        return "output"
    output_prefixes = (
        "q",
        "out",
        "dout",
        "data_out",
        "rddata",
        "rdata",
        "result",
    )
    output_suffixes = (
        "_q",
        "_o",
        "_od",
        "_out",
        "_do",
        "_y",
        "out",
    )
    if name.startswith(output_prefixes) or name.endswith(output_suffixes):
        return "output"
    return "input"


def _sanitize_netlistsvg_json_directions(json_path: Path) -> None:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return
    modules = data.get("modules", {})
    if not isinstance(modules, dict):
        return
    changed = False
    for _mod_name, mod in modules.items():
        if not isinstance(mod, dict):
            continue
        ports = mod.get("ports", {})
        if isinstance(ports, dict):
            for pname, p in ports.items():
                if not isinstance(p, dict):
                    continue
                direction = str(p.get("direction", "")).strip().lower()
                attrs = p.get("attributes", {})
                if not isinstance(attrs, dict):
                    attrs = {}
                if direction == "inout":
                    # netlistsvg schema only allows input/output, but we preserve the
                    # original intent for UI coloring and interaction.
                    attrs["rtlens_orig_direction"] = "inout"
                    p["attributes"] = attrs
                    p["direction"] = "output"
                    changed = True
                    continue
                if direction in {"input", "output"}:
                    continue
                p["direction"] = _guess_port_direction(str(pname))
                changed = True
        cells = mod.get("cells", {})
        if isinstance(cells, dict):
            for _cell_name, cell in cells.items():
                if not isinstance(cell, dict):
                    continue
                pdirs = cell.get("port_directions", {})
                if not isinstance(pdirs, dict):
                    continue
                for pname, pdir in list(pdirs.items()):
                    direction = str(pdir).strip().lower()
                    if direction in {"input", "output"}:
                        continue
                    pdirs[pname] = _guess_port_direction(str(pname))
                    changed = True
    if changed:
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _scan_yosys_unsupported(files: Iterable[str]) -> List[str]:
    issues: List[str] = []
    patterns = [
        (re.compile(r"^\s*import\s+[A-Za-z_][A-Za-z0-9_$]*::", re.M), "package import"),
    ]
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for rx, label in patterns:
            m = rx.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                issues.append(f"{os.path.abspath(path)}:{line}: unsupported by yosys frontend: {label}")
                break
    return issues


def generate_netlistsvg_view(
    files: Iterable[str],
    module_name: str,
    extra_args: Optional[Iterable[str]] = None,
    yosys_cmd: str = "yosys",
    netlistsvg_cmd: str = "netlistsvg",
    netlistsvg_dir: str = "",
    sv2v_cmd: str = "",
    timeout_sec: int = 25,
    progress_cb: Optional[Callable[[dict], None]] = None,
    heartbeat_sec: int = 5,
    progress_stage: str = "single",
) -> NetlistSvgResult:
    """Generate one module schematic (JSON/SVG/HTML) with fallback strategies.

    This function is used for on-demand schematic generation in UI actions.
    It prefers a direct yosys path first and optionally retries via sv2v when
    direct synthesis cannot resolve module dependencies.
    """
    overall_t0 = time.perf_counter()
    abs_files, dedup_stats = dedupe_existing_files_canonical(files)
    _emit_metric_event(
        progress_cb,
        stage=f"{progress_stage}.input_dedup",
        module=module_name,
        elapsed_sec=0.0,
        input_files=int(dedup_stats.get("input_files", 0)),
        unique_files=int(dedup_stats.get("unique_files", 0)),
        dropped_files=int(dedup_stats.get("dropped_files", 0)),
    )
    if not abs_files:
        return NetlistSvgResult(module_name=module_name, error="No RTL files loaded")
    if not module_name:
        return NetlistSvgResult(module_name=module_name, error="No module selected")
    sv2v_cmd = str(sv2v_cmd or "").strip()

    # Strategy:
    # 1) try yosys-direct first (fast path; avoids sv2v hangs on some modules),
    # 2) if that fails and sv2v is available, retry with sv2v conversion.
    if sv2v_cmd:
        direct = generate_netlistsvg_view(
            files=abs_files,
            module_name=module_name,
            extra_args=extra_args,
            yosys_cmd=yosys_cmd,
            netlistsvg_cmd=netlistsvg_cmd,
            netlistsvg_dir=netlistsvg_dir,
            sv2v_cmd="",
            timeout_sec=timeout_sec,
            progress_cb=progress_cb,
            heartbeat_sec=heartbeat_sec,
            progress_stage=progress_stage,
        )
        if not direct.error:
            direct.log = "[rtlens] strategy: yosys-direct (sv2v skipped)\n" + (direct.log or "")
            return direct

    parsed_db = None
    defined_macros = _extract_defined_macros(extra_args or [])
    t_parse = time.perf_counter()
    try:
        parsed_db = parse_sv_files(abs_files, defined_macros=defined_macros)
    except Exception:
        parsed_db = None
    _emit_metric_event(
        progress_cb,
        stage=f"{progress_stage}.parse_sv_files",
        module=module_name,
        elapsed_sec=max(0.0, time.perf_counter() - t_parse),
        files=len(abs_files),
    )
    yosys_files = _select_yosys_input_files(abs_files, module_name, parsed_db)
    sv2v_files = _select_sv2v_support_files(abs_files, module_name, parsed_db)
    read_args = _translate_slang_args_to_yosys(extra_args or [])
    missing: Set[str] = set()
    stub_ports: Dict[str, Set[str]] = {}
    stub_params: Dict[str, Set[str]] = {}
    filtered_stub_modules: Set[str] = set()
    if parsed_db is not None:
        for mod in parsed_db.modules.values():
            if mod.name not in _module_closure(parsed_db, module_name):
                continue
            for inst in mod.instances:
                inst_mod = _normalize_module_symbol(inst.module_type)
                if not inst_mod:
                    continue
                if not _is_valid_blackbox_module_symbol(inst_mod):
                    filtered_stub_modules.add(inst_mod)
                    continue
                if inst_mod in parsed_db.modules:
                    continue
                missing.add(inst_mod)
                stub_ports.setdefault(inst_mod, set()).update(inst.connections.keys())
                if inst.parameters:
                    stub_params.setdefault(inst_mod, set()).update(inst.parameters.keys())
    if filtered_stub_modules:
        _emit_metric_event(
            progress_cb,
            stage=f"{progress_stage}.stub_filter",
            module=module_name,
            elapsed_sec=0.0,
            filtered_count=len(filtered_stub_modules),
            filtered_sample=",".join(sorted(filtered_stub_modules)[:5]),
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="rtlens_netlistsvg_"))
    json_path = tmpdir / f"{module_name}.json"
    svg_path = tmpdir / f"{module_name}.svg"
    html_path = tmpdir / f"{module_name}.html"
    quoted_read = " ".join(_yosys_quote_arg(a) for a in read_args)
    input_files_for_yosys = list(yosys_files)
    sv2v_log = ""
    line_map: Dict[int, tuple[str, int]] = {}
    sv2v_out: Optional[Path] = None
    sv2v_base_args: List[str] = []
    sv2v_selected_files: List[str] = list(sv2v_files)
    sv2v_missing_retry_done = False

    if sv2v_cmd:
        sv2v_out = tmpdir / f"{module_name}.sv2v.v"
        sv2v_base_args = [sv2v_cmd] + _translate_slang_args_to_sv2v(extra_args or []) + ["--top", module_name]
        sv2v_selected_files = list(sv2v_files)
        sv2v_attempt_logs: List[str] = []
        sv2v_proc = None
        sv2v_ok = False
        max_attempts = 8
        minimal_files = _select_sv2v_minimal_files(abs_files, module_name, parsed_db)
        if minimal_files and len(minimal_files) < len(sv2v_selected_files):
            sv2v_attempt_logs.append(
                "[rtlens] sv2v seed reduction\n"
                f"module: {module_name}\n"
                f"seed files: {len(sv2v_selected_files)} -> {len(minimal_files)}"
            )
            sv2v_selected_files = minimal_files
        for attempt in range(1, max_attempts + 1):
            sv2v_args = sv2v_base_args + sv2v_selected_files
            t_sv2v = time.perf_counter()
            sv2v_proc, sv2v_timeout = _run_command_with_heartbeat(
                sv2v_args,
                timeout_sec=timeout_sec,
                progress_cb=progress_cb,
                heartbeat_sec=heartbeat_sec,
                progress_meta={
                    "tool": "sv2v",
                    "module": module_name,
                    "stage": f"{progress_stage}.sv2v",
                    "attempt": attempt,
                    "attempts": max_attempts,
                },
            )
            _emit_metric_event(
                progress_cb,
                stage=f"{progress_stage}.sv2v.attempt",
                module=module_name,
                elapsed_sec=max(0.0, time.perf_counter() - t_sv2v),
                attempt=attempt,
                attempts=max_attempts,
                file_count=len(sv2v_selected_files),
            )
            if sv2v_timeout is not None:
                e = sv2v_timeout
                timeout_log = "\n".join(
                    [
                        f"sv2v attempt {attempt}/{max_attempts}",
                        f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_args)}",
                        f"sv2v timeout after {timeout_sec}s",
                        "sv2v stdout:",
                        _coerce_text_blob(e.stdout).strip() or "(none)",
                        "sv2v stderr:",
                        _coerce_text_blob(e.stderr).strip() or "(none)",
                    ]
                )
                sv2v_attempt_logs.append(timeout_log)
                if attempt == 1 and minimal_files and len(minimal_files) < len(sv2v_selected_files):
                    sv2v_selected_files = list(minimal_files)
                    continue
                return NetlistSvgResult(
                    module_name=module_name,
                    error=f"sv2v timeout after {timeout_sec}s",
                    log="[rtlens] netlistsvg view\nmodule: "
                    + module_name
                    + "\n"
                    + "\n\n".join(sv2v_attempt_logs),
                )

            merged = (sv2v_proc.stdout or "") + "\n" + (sv2v_proc.stderr or "")
            missing_mods = _extract_sv2v_missing_modules(merged)
            added_mods, added_files = _expand_sv2v_support_files(parsed_db, missing_mods, sv2v_selected_files)
            attempt_log = [
                f"sv2v attempt {attempt}/{max_attempts}",
                f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_args)}",
                "sv2v stdout:",
                sv2v_proc.stdout.strip() if sv2v_proc.stdout.strip() else "(none)",
                "sv2v stderr:",
                sv2v_proc.stderr.strip() if sv2v_proc.stderr.strip() else "(none)",
                f"sv2v return code: {sv2v_proc.returncode}",
            ]
            if sv2v_proc.returncode == 0 and sv2v_proc.stdout.strip():
                sv2v_ok = True
                sv2v_attempt_logs.append("\n".join(attempt_log))
                break
            if missing_mods:
                attempt_log.append(f"sv2v missing modules: {', '.join(sorted(missing_mods))}")
            if added_mods:
                attempt_log.append(f"sv2v added modules: {', '.join(added_mods)}")
            if added_files:
                sv2v_selected_files.extend(added_files)
                attempt_log.append(f"sv2v added files: {len(added_files)}")
            sv2v_attempt_logs.append("\n".join(attempt_log))
            if not added_files:
                break

        sv2v_log = "\n\n".join(sv2v_attempt_logs)
        if not sv2v_ok or sv2v_proc is None:
            return NetlistSvgResult(
                module_name=module_name,
                error="sv2v failed before yosys.\n" + sv2v_log,
                log="[rtlens] netlistsvg view\nmodule: " + module_name + "\n" + sv2v_log,
            )
        sv2v_out.write_text(sv2v_proc.stdout, encoding="utf-8")
        input_files_for_yosys = [str(sv2v_out)]
        sv2v_files = list(sv2v_selected_files)
        line_map = _build_sv2v_line_map(str(sv2v_out), sv2v_files)
        missing.clear()
        stub_ports.clear()
    else:
        unsupported = _scan_yosys_unsupported(yosys_files)
        if unsupported:
            return NetlistSvgResult(
                module_name=module_name,
                error="Yosys frontend does not support one or more SystemVerilog constructs in this module closure.\n"
                + "\n".join(unsupported),
                log="[rtlens] netlistsvg view\nmodule: "
                + module_name
                + "\nunsupported constructs detected before yosys run:\n"
                + "\n".join(unsupported),
            )

    proc = None
    script: List[str] = []
    for _attempt in range(8):
        stub_file = ""
        valid_missing = [mod for mod in sorted(missing) if _is_valid_blackbox_module_symbol(mod)]
        if valid_missing:
            stub_path = tmpdir / "rtlens_blackbox_stubs.v"
            with open(stub_path, "w", encoding="utf-8") as f:
                for mod in valid_missing:
                    ports = sorted(stub_ports.get(mod, set()))
                    params = sorted(stub_params.get(mod, set()))
                    if params:
                        pdecl = ", ".join(f"parameter {p} = 0" for p in params)
                        if ports:
                            plist = ", ".join(ports)
                            f.write(f"(* blackbox *) module {mod} #({pdecl}) ({plist});\n")
                        else:
                            f.write(f"(* blackbox *) module {mod} #({pdecl}) (); endmodule\n")
                            continue
                    elif ports:
                        plist = ", ".join(ports)
                        f.write(f"(* blackbox *) module {mod}({plist});\n")
                    else:
                        f.write(f"(* blackbox *) module {mod}(); endmodule\n")
                        continue
                    for p in ports:
                        direction = _guess_port_direction(p)
                        f.write(f"  {direction} {p};\n")
                    f.write("endmodule\n")
            stub_file = str(stub_path)
        read_parts = [f"read_verilog -sv -defer {quoted_read}".strip()]
        read_parts.extend(_yosys_quote_arg(_normalize_tool_path(f)) for f in input_files_for_yosys)
        if stub_file:
            read_parts.append(";")
            read_parts.append(
                f"read_verilog -sv -lib {_yosys_quote_arg(_normalize_tool_path(stub_file))}"
            )
        script = [
            " ".join(read_parts),
            f"prep -top {_yosys_quote_arg(module_name)}",
            f"write_json {_yosys_quote_arg(_normalize_tool_path(str(json_path)))}",
        ]
        run_cmd = [yosys_cmd, "-Q", "-p", "; ".join(script)]
        t_yosys = time.perf_counter()
        proc, yosys_timeout = _run_command_with_heartbeat(
            run_cmd,
            timeout_sec=timeout_sec,
            progress_cb=progress_cb,
            heartbeat_sec=heartbeat_sec,
            progress_meta={
                "tool": "yosys",
                "module": module_name,
                "stage": f"{progress_stage}.yosys",
            },
        )
        _emit_metric_event(
            progress_cb,
            stage=f"{progress_stage}.yosys.attempt",
            module=module_name,
            elapsed_sec=max(0.0, time.perf_counter() - t_yosys),
            script_len=len(script),
        )
        if yosys_timeout is not None:
            e = yosys_timeout
            return NetlistSvgResult(
                module_name=module_name,
                error=f"yosys timeout after {timeout_sec}s",
                log="[rtlens] netlistsvg view\nmodule: "
                + module_name
                + "\nyosys timeout\n"
                + _coerce_text_blob(e.stdout)
                + "\n"
                + _coerce_text_blob(e.stderr),
            )
        if proc.returncode == 0:
            break
        merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
        new_missing = _extract_missing_modules(merged)
        if (
            sv2v_cmd
            and sv2v_out is not None
            and sv2v_base_args
            and parsed_db is not None
            and new_missing
            and not sv2v_missing_retry_done
        ):
            added_mods, added_files = _expand_sv2v_support_files(parsed_db, new_missing, sv2v_selected_files)
            if added_files:
                sv2v_missing_retry_done = True
                sv2v_selected_files.extend(added_files)
                sv2v_retry_cmd = sv2v_base_args + sv2v_selected_files
                t_sv2v_retry = time.perf_counter()
                sv2v_retry_proc, sv2v_retry_timeout = _run_command_with_heartbeat(
                    sv2v_retry_cmd,
                    timeout_sec=timeout_sec,
                    progress_cb=progress_cb,
                    heartbeat_sec=heartbeat_sec,
                    progress_meta={
                        "tool": "sv2v",
                        "module": module_name,
                        "stage": f"{progress_stage}.sv2v-yosys-missing-retry",
                    },
                )
                _emit_metric_event(
                    progress_cb,
                    stage=f"{progress_stage}.sv2v.yosys_missing_retry",
                    module=module_name,
                    elapsed_sec=max(0.0, time.perf_counter() - t_sv2v_retry),
                    file_count=len(sv2v_selected_files),
                    missing_count=len(new_missing),
                )
                retry_log_lines = [
                    "[rtlens] sv2v retry for yosys missing modules",
                    f"missing modules: {', '.join(sorted(new_missing))}",
                    f"added modules: {', '.join(added_mods) if added_mods else '(none)'}",
                    f"added files: {len(added_files)}",
                    f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_retry_cmd)}",
                ]
                if sv2v_retry_timeout is not None:
                    e = sv2v_retry_timeout
                    retry_log_lines.extend(
                        [
                            f"sv2v timeout after {timeout_sec}s",
                            "sv2v stdout:",
                            _coerce_text_blob(e.stdout).strip() or "(none)",
                            "sv2v stderr:",
                            _coerce_text_blob(e.stderr).strip() or "(none)",
                        ]
                    )
                else:
                    retry_log_lines.extend(
                        [
                            "sv2v stdout:",
                            (sv2v_retry_proc.stdout or "").strip() or "(none)",
                            "sv2v stderr:",
                            (sv2v_retry_proc.stderr or "").strip() or "(none)",
                            f"sv2v return code: {sv2v_retry_proc.returncode}",
                        ]
                    )
                sv2v_log = (sv2v_log + "\n\n" if sv2v_log else "") + "\n".join(retry_log_lines)
                if (
                    sv2v_retry_timeout is None
                    and sv2v_retry_proc is not None
                    and sv2v_retry_proc.returncode == 0
                    and (sv2v_retry_proc.stdout or "").strip()
                ):
                    sv2v_out.write_text(sv2v_retry_proc.stdout, encoding="utf-8")
                    input_files_for_yosys = [str(sv2v_out)]
                    sv2v_files = list(sv2v_selected_files)
                    line_map = _build_sv2v_line_map(str(sv2v_out), sv2v_files)
                    missing.clear()
                    stub_ports.clear()
                    stub_params.clear()
                    continue
        missing_ports = _extract_missing_ports(merged)
        for mod, ports in missing_ports.items():
            stub_ports.setdefault(mod, set()).update(ports)
        missing_params = _extract_missing_parameters(merged)
        for mod, params in missing_params.items():
            stub_params.setdefault(mod, set()).update(params)
        positional_requests = _extract_positional_port_requests(merged)
        for mod, count in positional_requests.items():
            ports = stub_ports.setdefault(mod, set())
            for idx in range(max(0, int(count))):
                ports.add(f"p{idx}")
        if (new_missing or missing_ports or missing_params or positional_requests) and parsed_db is not None:
            _fill_stub_ports_from_instances(
                parsed_db,
                new_missing | missing | set(missing_ports.keys()) | set(missing_params.keys()) | set(positional_requests.keys()),
                stub_ports,
                stub_params,
            )
        missing.update(missing_params.keys())
        missing.update(positional_requests.keys())
        new_missing -= missing
        if not new_missing and not missing_ports and not missing_params and not positional_requests:
            break
        missing.update(new_missing)

    log_lines = [
        "[rtlens] netlistsvg view",
        f"module: {module_name}",
        f"yosys command: {yosys_cmd} -Q -p {'; '.join(script)}",
        f"return code: {proc.returncode if proc is not None else 'n/a'}",
        (
            "file dedup: "
            f"input={int(dedup_stats.get('input_files', 0))} "
            f"unique={int(dedup_stats.get('unique_files', 0))} "
            f"dropped={int(dedup_stats.get('dropped_files', 0))}"
        ),
        f"input files: {len(input_files_for_yosys)} / {len(abs_files)}",
        f"sv2v support files: {len(sv2v_files)}",
        f"blackbox stubs: {', '.join(sorted(missing)) if missing else '(none)'}",
        "stdout:",
        proc.stdout.strip() if proc and proc.stdout.strip() else "(none)",
        "stderr:",
        proc.stderr.strip() if proc and proc.stderr.strip() else "(none)",
    ]
    if sv2v_log:
        log_lines.extend(["sv2v:", sv2v_log])
    result = NetlistSvgResult(module_name=module_name, json_path=str(json_path), svg_path=str(svg_path), html_path=str(html_path), log="\n".join(log_lines))
    if proc is None or proc.returncode != 0 or not json_path.is_file():
        result.error = (proc.stderr.strip() if proc and proc.stderr else "") or "yosys failed"
        return result

    _augment_yosys_json(json_path, parsed_db, module_name, line_map)
    _sanitize_netlistsvg_json_directions(json_path)

    svg_proc = None
    candidate_logs: List[str] = []
    for cmd in _netlistsvg_command_candidates(netlistsvg_dir, netlistsvg_cmd):
        run_cmd = cmd + [str(json_path), "-o", str(svg_path)]
        t_netlistsvg = time.perf_counter()
        try:
            cur, netlistsvg_timeout = _run_command_with_heartbeat(
                run_cmd,
                timeout_sec=timeout_sec,
                progress_cb=progress_cb,
                heartbeat_sec=heartbeat_sec,
                progress_meta={
                    "tool": "netlistsvg",
                    "module": module_name,
                    "stage": f"{progress_stage}.netlistsvg",
                },
            )
        except Exception as e:
            _emit_metric_event(
                progress_cb,
                stage=f"{progress_stage}.netlistsvg.attempt",
                module=module_name,
                elapsed_sec=max(0.0, time.perf_counter() - t_netlistsvg),
            )
            candidate_logs.append(
                "netlistsvg command: "
                + " ".join(shlex.quote(x) for x in run_cmd)
                + "\nnetlistsvg launch failed:\n"
                + str(e)
            )
            continue
        _emit_metric_event(
            progress_cb,
            stage=f"{progress_stage}.netlistsvg.attempt",
            module=module_name,
            elapsed_sec=max(0.0, time.perf_counter() - t_netlistsvg),
        )
        if netlistsvg_timeout is not None:
            e = netlistsvg_timeout
            candidate_logs.append(
                "netlistsvg command: "
                + " ".join(shlex.quote(x) for x in run_cmd)
                + f"\nnetlistsvg timeout after {timeout_sec}s\n"
                + _coerce_text_blob(e.stdout)
                + "\n"
                + _coerce_text_blob(e.stderr)
            )
            continue
        candidate_logs.append(
            "netlistsvg command: "
            + " ".join(shlex.quote(x) for x in run_cmd)
            + "\nnetlistsvg stdout:\n"
            + (cur.stdout.strip() if cur and cur.stdout else "(none)")
            + "\nnetlistsvg stderr:\n"
            + (cur.stderr.strip() if cur and cur.stderr else "(none)")
        )
        if cur is not None and cur.returncode == 0 and svg_path.is_file():
            svg_proc = cur
            break
        svg_proc = cur
    result.log += "\n" + "\n\n".join(candidate_logs)
    if svg_proc is None or svg_proc.returncode != 0 or not svg_path.is_file():
        result.error = (svg_proc.stderr.strip() if svg_proc and svg_proc.stderr else "") or "netlistsvg failed"
        return result

    t_post_svg = time.perf_counter()
    svg_text = svg_path.read_text(encoding="utf-8", errors="ignore")
    svg_text = _inject_svg_data_src_from_json(svg_text, json_path, module_name)
    svg_text = _inline_svg_styles_for_qt(svg_text)
    svg_path.write_text(svg_text, encoding="utf-8")
    _build_html(svg_text, yosys_files, html_path)
    _emit_metric_event(
        progress_cb,
        stage=f"{progress_stage}.post_svg_html",
        module=module_name,
        elapsed_sec=max(0.0, time.perf_counter() - t_post_svg),
    )
    _emit_metric_event(
        progress_cb,
        stage=f"{progress_stage}.total",
        module=module_name,
        elapsed_sec=max(0.0, time.perf_counter() - overall_t0),
    )
    return result


def _resolve_json_module_key_for_instance(
    modules: Dict[str, dict],
    top_module_key: str,
    rel_instance_names: List[str],
) -> str:
    if not top_module_key or top_module_key not in modules:
        return ""
    if not rel_instance_names:
        return top_module_key
    canonical_map: Dict[str, str] = {}
    for key in modules.keys():
        canonical_map.setdefault(_canonical_module_name(key), key)
    cur_key = top_module_key
    idx = 0
    chain = [str(x or "").strip() for x in rel_instance_names]
    while idx < len(chain):
        inst_name = chain[idx]
        mod_obj = modules.get(cur_key, {})
        cells = mod_obj.get("cells", {}) if isinstance(mod_obj, dict) else {}
        if not isinstance(cells, dict):
            return ""
        canonical_cells: Dict[str, str] = {}
        for cell_name in cells.keys():
            canonical_cells.setdefault(_canonical_module_name(str(cell_name)), str(cell_name))

        target_cell = ""
        consumed = 1
        canon_inst = _canonical_module_name(inst_name)
        if canon_inst in canonical_cells:
            target_cell = canonical_cells[canon_inst]
        else:
            # Some generated scopes appear as dotted single cell names in yosys
            # (e.g. "genblk2.genblk1.lowMask_roundMask"). Match the longest
            # dotted suffix from the remaining chain.
            for span in range(len(chain) - idx, 1, -1):
                dotted = ".".join(_canonical_module_name(x) for x in chain[idx : idx + span] if str(x).strip())
                if dotted and dotted in canonical_cells:
                    target_cell = canonical_cells[dotted]
                    consumed = span
                    break
        if not target_cell:
            # Generated scope labels (e.g. genblk*) may exist in parsed
            # hierarchy path but not as actual yosys cell names. Skip them.
            if re.match(r"^genblk\d+$", str(inst_name or "").strip()):
                idx += 1
                continue
            return ""
        cell_obj = cells.get(target_cell, {})
        cell_type = _canonical_module_name(str((cell_obj or {}).get("type", "")))
        if not cell_type:
            return ""
        next_key = canonical_map.get(cell_type, "")
        if not next_key:
            return ""
        cur_key = next_key
        idx += max(1, int(consumed))
    return cur_key


def _json_module_closure(modules: Dict[str, dict], start_key: str) -> Set[str]:
    out: Set[str] = set()
    if not start_key or start_key not in modules:
        return out
    canonical_map: Dict[str, str] = {}
    for key in modules.keys():
        canonical_map.setdefault(_canonical_module_name(key), key)
    stack: List[str] = [start_key]
    while stack:
        cur = stack.pop()
        if cur in out or cur not in modules:
            continue
        out.add(cur)
        cells = modules.get(cur, {}).get("cells", {}) or {}
        for cell in cells.values():
            ctype = _canonical_module_name(str((cell or {}).get("type", "")))
            nxt = canonical_map.get(ctype, "")
            if nxt and nxt not in out:
                stack.append(nxt)
    return out


def _slice_yosys_json_for_module(full_data: dict, module_key: str) -> dict:
    modules = full_data.get("modules", {}) if isinstance(full_data, dict) else {}
    if not isinstance(modules, dict) or module_key not in modules:
        return {"modules": {}}
    keep = _json_module_closure(modules, module_key)
    sliced: dict = {}
    for k, v in full_data.items():
        if k == "modules":
            continue
        sliced[k] = v
    sliced_modules: Dict[str, dict] = {}
    for key in keep:
        try:
            obj = json.loads(json.dumps(modules[key]))
        except Exception:
            obj = dict(modules[key]) if isinstance(modules[key], dict) else {}
        attrs = obj.setdefault("attributes", {}) if isinstance(obj, dict) else {}
        if isinstance(attrs, dict):
            attrs.pop("top", None)
        sliced_modules[key] = obj
    top_attrs = sliced_modules.get(module_key, {}).setdefault("attributes", {})
    if isinstance(top_attrs, dict):
        top_attrs["top"] = "00000000000000000000000000000001"
    sliced["modules"] = sliced_modules
    return sliced


def generate_netlistsvg_prebuild_batch(
    files: Iterable[str],
    top_module: str,
    module_names: Optional[Iterable[str]] = None,
    instance_requests: Optional[Dict[str, List[str]]] = None,
    extra_args: Optional[Iterable[str]] = None,
    yosys_cmd: str = "yosys",
    netlistsvg_cmd: str = "netlistsvg",
    netlistsvg_dir: str = "",
    sv2v_cmd: str = "",
    timeout_sec: int = 25,
    progress_cb: Optional[Callable[[dict], None]] = None,
    heartbeat_sec: int = 5,
    top_cache_key: str = "",
    top_cache_dir: str = "",
) -> Dict[str, NetlistSvgResult]:
    """Prebuild schematics for a set of requested modules/instances.

    Returns:
        Mapping from request key (module name or instance request key) to
        :class:`NetlistSvgResult`.
    """
    batch_t0 = time.perf_counter()
    request_mode = "module"
    req_rel_instances: Dict[str, List[str]] = {}
    requested: List[str] = []
    if instance_requests:
        request_mode = "instance"
        for req_key, rel_names in instance_requests.items():
            req = str(req_key or "").strip()
            if not req:
                continue
            requested.append(req)
            rel_clean: List[str] = []
            for raw in list(rel_names or []):
                name = str(raw or "").strip()
                if name:
                    rel_clean.append(name)
            req_rel_instances[req] = rel_clean
    else:
        requested = [str(x or "").strip() for x in list(module_names or []) if str(x or "").strip()]
    requested_unique: List[str] = []
    seen_req: Set[str] = set()
    for mod in requested:
        if mod in seen_req:
            continue
        seen_req.add(mod)
        requested_unique.append(mod)
    out: Dict[str, NetlistSvgResult] = {mod: NetlistSvgResult(module_name=mod) for mod in requested_unique}
    if not requested_unique:
        return out
    top_module = str(top_module or "").strip()
    abs_files, dedup_stats = dedupe_existing_files_canonical(files)
    _emit_metric_event(
        progress_cb,
        stage="batch.input_dedup",
        module=top_module,
        elapsed_sec=0.0,
        input_files=int(dedup_stats.get("input_files", 0)),
        unique_files=int(dedup_stats.get("unique_files", 0)),
        dropped_files=int(dedup_stats.get("dropped_files", 0)),
    )
    if not abs_files:
        err = "No RTL files loaded"
        for mod in requested_unique:
            out[mod].error = err
            out[mod].log = "[rtlens] schematic prebuild batch\nerror: " + err
        return out
    if not top_module:
        err = "No top module selected for schematic prebuild"
        for mod in requested_unique:
            out[mod].error = err
            out[mod].log = "[rtlens] schematic prebuild batch\nerror: " + err
        return out

    parsed_db = None
    defined_macros = _extract_defined_macros(extra_args or [])
    t_parse = time.perf_counter()
    try:
        parsed_db = parse_sv_files(abs_files, defined_macros=defined_macros)
    except Exception:
        parsed_db = None
    _emit_metric_event(
        progress_cb,
        stage="batch.parse_sv_files",
        module=top_module,
        elapsed_sec=max(0.0, time.perf_counter() - t_parse),
        files=len(abs_files),
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="rtlens_schematic_prebuild_"))
    quoted_read = " ".join(_yosys_quote_arg(a) for a in _translate_slang_args_to_yosys(extra_args or []))
    sv2v_line_map: Dict[int, tuple[str, int]] = {}
    sv2v_log = ""
    strategy = "yosys-direct"
    full_json_path = tmpdir / f"{_canonical_module_name(top_module) or 'top'}.full.json"
    reuse_json_path: Optional[Path] = None
    if top_cache_key and top_cache_dir:
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(top_cache_key or "")).strip("._")
        if safe_key:
            reuse_root = Path(str(top_cache_dir)).expanduser()
            reuse_json_path = reuse_root / "top_sessions" / f"{safe_key}.full.json"

    def _run_yosys_with_inputs(input_files_for_yosys: List[str]) -> tuple[bool, str, str]:
        missing: Set[str] = set()
        stub_ports: Dict[str, Set[str]] = {}
        stub_params: Dict[str, Set[str]] = {}
        filtered_stub_modules: Set[str] = set()
        if parsed_db is not None:
            closure = _module_closure(parsed_db, top_module) if top_module in parsed_db.modules else set(parsed_db.modules.keys())
            for mod in parsed_db.modules.values():
                if closure and mod.name not in closure:
                    continue
                for inst in mod.instances:
                    inst_mod = _normalize_module_symbol(inst.module_type)
                    if not inst_mod:
                        continue
                    if not _is_valid_blackbox_module_symbol(inst_mod):
                        filtered_stub_modules.add(inst_mod)
                        continue
                    if inst_mod in parsed_db.modules:
                        continue
                    missing.add(inst_mod)
                    stub_ports.setdefault(inst_mod, set()).update(inst.connections.keys())
                    if inst.parameters:
                        stub_params.setdefault(inst_mod, set()).update(inst.parameters.keys())
        if filtered_stub_modules:
            _emit_metric_event(
                progress_cb,
                stage="batch.stub_filter",
                module=top_module,
                elapsed_sec=0.0,
                filtered_count=len(filtered_stub_modules),
                filtered_sample=",".join(sorted(filtered_stub_modules)[:5]),
            )
        proc = None
        script: List[str] = []
        for _attempt in range(8):
            stub_file = ""
            valid_missing = [mod for mod in sorted(missing) if _is_valid_blackbox_module_symbol(mod)]
            if valid_missing:
                stub_path = tmpdir / "rtlens_prebuild_blackbox_stubs.v"
                with open(stub_path, "w", encoding="utf-8") as f:
                    for mod in valid_missing:
                        ports = sorted(stub_ports.get(mod, set()))
                        params = sorted(stub_params.get(mod, set()))
                        if params:
                            pdecl = ", ".join(f"parameter {p} = 0" for p in params)
                            if ports:
                                plist = ", ".join(ports)
                                f.write(f"(* blackbox *) module {mod} #({pdecl}) ({plist});\n")
                            else:
                                f.write(f"(* blackbox *) module {mod} #({pdecl}) (); endmodule\n")
                                continue
                        elif ports:
                            plist = ", ".join(ports)
                            f.write(f"(* blackbox *) module {mod}({plist});\n")
                        else:
                            f.write(f"(* blackbox *) module {mod}(); endmodule\n")
                            continue
                        for p in ports:
                            direction = _guess_port_direction(p)
                            f.write(f"  {direction} {p};\n")
                        f.write("endmodule\n")
                stub_file = str(stub_path)
            read_parts = [f"read_verilog -sv -defer {quoted_read}".strip()]
            read_parts.extend(_yosys_quote_arg(_normalize_tool_path(f)) for f in input_files_for_yosys)
            if stub_file:
                read_parts.append(";")
                read_parts.append(
                    f"read_verilog -sv -lib {_yosys_quote_arg(_normalize_tool_path(stub_file))}"
                )
            script = [
                " ".join(read_parts),
                f"hierarchy -check -top {_yosys_quote_arg(top_module)}",
                f"prep -top {_yosys_quote_arg(top_module)}",
                f"write_json {_yosys_quote_arg(_normalize_tool_path(str(full_json_path)))}",
            ]
            run_cmd = [yosys_cmd, "-Q", "-p", "; ".join(script)]
            t_yosys = time.perf_counter()
            try:
                proc, yosys_timeout = _run_command_with_heartbeat(
                    run_cmd,
                    timeout_sec=timeout_sec,
                    progress_cb=progress_cb,
                    heartbeat_sec=heartbeat_sec,
                    progress_meta={
                        "tool": "yosys",
                        "module": top_module,
                        "stage": "batch.yosys-top",
                    },
                )
            except Exception as e:
                _emit_metric_event(
                    progress_cb,
                    stage="batch.yosys_top.attempt",
                    module=top_module,
                    elapsed_sec=max(0.0, time.perf_counter() - t_yosys),
                    input_files=len(input_files_for_yosys),
                )
                fail_log = (
                    f"yosys command: {yosys_cmd} -Q -p {'; '.join(script)}\n"
                    f"yosys launch failed: {e}"
                )
                return False, fail_log, f"yosys launch failed: {e}"
            _emit_metric_event(
                progress_cb,
                stage="batch.yosys_top.attempt",
                module=top_module,
                elapsed_sec=max(0.0, time.perf_counter() - t_yosys),
                input_files=len(input_files_for_yosys),
            )
            if yosys_timeout is not None:
                e = yosys_timeout
                timeout_log = (
                    f"yosys command: {yosys_cmd} -Q -p {'; '.join(script)}\n"
                    f"yosys timeout after {timeout_sec}s\n"
                    + _coerce_text_blob(e.stdout)
                    + "\n"
                    + _coerce_text_blob(e.stderr)
                )
                return False, timeout_log, f"yosys timeout after {timeout_sec}s"
            if proc.returncode == 0 and full_json_path.is_file():
                summary = "\n".join(
                    [
                        f"yosys command: {yosys_cmd} -Q -p {'; '.join(script)}",
                        f"return code: {proc.returncode}",
                        f"input files: {len(input_files_for_yosys)} / {len(abs_files)}",
                        f"blackbox stubs: {', '.join(sorted(missing)) if missing else '(none)'}",
                        "stdout:",
                        proc.stdout.strip() if proc.stdout.strip() else "(none)",
                        "stderr:",
                        proc.stderr.strip() if proc.stderr.strip() else "(none)",
                    ]
                )
                return True, summary, ""
            merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
            new_missing = _extract_missing_modules(merged)
            missing_ports = _extract_missing_ports(merged)
            for mod, ports in missing_ports.items():
                stub_ports.setdefault(mod, set()).update(ports)
            missing_params = _extract_missing_parameters(merged)
            for mod, params in missing_params.items():
                stub_params.setdefault(mod, set()).update(params)
            positional_requests = _extract_positional_port_requests(merged)
            for mod, count in positional_requests.items():
                ports = stub_ports.setdefault(mod, set())
                for idx in range(max(0, int(count))):
                    ports.add(f"p{idx}")
            if (new_missing or missing_ports or missing_params or positional_requests) and parsed_db is not None:
                _fill_stub_ports_from_instances(
                    parsed_db,
                    new_missing | missing | set(missing_ports.keys()) | set(missing_params.keys()) | set(positional_requests.keys()),
                    stub_ports,
                    stub_params,
                )
            missing.update(missing_params.keys())
            missing.update(positional_requests.keys())
            new_missing -= missing
            if not new_missing and not missing_ports and not missing_params and not positional_requests:
                break
            missing.update(new_missing)
        fail_log = "\n".join(
            [
                f"yosys command: {yosys_cmd} -Q -p {'; '.join(script)}",
                f"return code: {proc.returncode if proc is not None else 'n/a'}",
                "stdout:",
                proc.stdout.strip() if proc and proc.stdout.strip() else "(none)",
                "stderr:",
                proc.stderr.strip() if proc and proc.stderr.strip() else "(none)",
            ]
        )
        err = (proc.stderr.strip() if proc and proc.stderr else "") or "yosys failed"
        return False, fail_log, err

    direct_ok = False
    top_log_detail = ""
    top_error = ""
    if reuse_json_path and reuse_json_path.is_file():
        strategy = "reuse-full-json"
        try:
            shutil.copy2(reuse_json_path, full_json_path)
            direct_ok = True
            top_log_detail = (
                "top synthesis cache reused\n"
                f"cache key: {top_cache_key}\n"
                f"full json: {str(reuse_json_path)}"
            )
        except Exception as e:
            top_error = f"failed to reuse top synthesis cache: {e}"
            top_log_detail = top_error
            direct_ok = False
    if not direct_ok:
        unsupported = _scan_yosys_unsupported(abs_files)
        if not unsupported:
            t_yosys_direct = time.perf_counter()
            direct_ok, top_log_detail, top_error = _run_yosys_with_inputs(abs_files)
            _emit_metric_event(
                progress_cb,
                stage="batch.yosys_top.direct_total",
                module=top_module,
                elapsed_sec=max(0.0, time.perf_counter() - t_yosys_direct),
                input_files=len(abs_files),
            )
        else:
            top_error = "Yosys frontend does not support one or more SystemVerilog constructs in full filelist closure."
            top_log_detail = "unsupported constructs detected before yosys run:\n" + "\n".join(unsupported)

    if (not direct_ok) and sv2v_cmd:
        strategy = "sv2v+yosys"
        sv2v_out = tmpdir / f"{_canonical_module_name(top_module) or 'top'}.sv2v.v"
        sv2v_args = [sv2v_cmd] + _translate_slang_args_to_sv2v(extra_args or []) + ["--top", top_module] + abs_files
        t_sv2v_top = time.perf_counter()
        try:
            sv2v_proc, sv2v_timeout = _run_command_with_heartbeat(
                sv2v_args,
                timeout_sec=timeout_sec,
                progress_cb=progress_cb,
                heartbeat_sec=heartbeat_sec,
                progress_meta={
                    "tool": "sv2v",
                    "module": top_module,
                    "stage": "batch.sv2v-top",
                },
            )
        except Exception as e:
            _emit_metric_event(
                progress_cb,
                stage="batch.sv2v_top",
                module=top_module,
                elapsed_sec=max(0.0, time.perf_counter() - t_sv2v_top),
                file_count=len(abs_files),
            )
            sv2v_log = (
                f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_args)}\n"
                f"sv2v launch failed: {e}"
            )
            top_error = f"sv2v launch failed: {e}"
            top_log_detail = sv2v_log
            sv2v_proc = None
        else:
            _emit_metric_event(
                progress_cb,
                stage="batch.sv2v_top",
                module=top_module,
                elapsed_sec=max(0.0, time.perf_counter() - t_sv2v_top),
                file_count=len(abs_files),
            )
            if sv2v_timeout is not None:
                e = sv2v_timeout
                sv2v_log = (
                    f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_args)}\n"
                    f"sv2v timeout after {timeout_sec}s\n"
                    + _coerce_text_blob(e.stdout)
                    + "\n"
                    + _coerce_text_blob(e.stderr)
                )
                top_error = f"sv2v timeout after {timeout_sec}s"
                top_log_detail = sv2v_log
                sv2v_proc = None
            else:
                sv2v_log = "\n".join(
                    [
                        f"sv2v command: {' '.join(shlex.quote(x) for x in sv2v_args)}",
                        "sv2v stdout:",
                        sv2v_proc.stdout.strip() if sv2v_proc.stdout.strip() else "(none)",
                        "sv2v stderr:",
                        sv2v_proc.stderr.strip() if sv2v_proc.stderr.strip() else "(none)",
                        f"sv2v return code: {sv2v_proc.returncode}",
                    ]
                )
        if sv2v_proc is not None and sv2v_proc.returncode == 0 and (sv2v_proc.stdout or "").strip():
            sv2v_out.write_text(sv2v_proc.stdout, encoding="utf-8")
            sv2v_line_map = _build_sv2v_line_map(str(sv2v_out), abs_files)
            t_yosys_sv2v = time.perf_counter()
            direct_ok, top_log_detail, top_error = _run_yosys_with_inputs([str(sv2v_out)])
            _emit_metric_event(
                progress_cb,
                stage="batch.yosys_top.after_sv2v_total",
                module=top_module,
                elapsed_sec=max(0.0, time.perf_counter() - t_yosys_sv2v),
                input_files=1,
            )
        elif sv2v_proc is not None:
            top_error = "sv2v failed before yosys"
            top_log_detail = sv2v_log
        else:
            if not top_error:
                top_error = "sv2v failed before yosys"
            if not top_log_detail:
                top_log_detail = sv2v_log

    if direct_ok and reuse_json_path is not None and not reuse_json_path.is_file():
        try:
            reuse_json_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full_json_path, reuse_json_path)
        except Exception:
            pass

    synth_log_header = "\n".join(
        [
            "[rtlens] schematic prebuild batch",
            f"strategy: {strategy}",
            f"top module: {top_module}",
            f"target requests: {len(requested_unique)}",
            f"request mode: {request_mode}",
            f"rtl files: {len(abs_files)}",
        ]
    )
    if sv2v_log:
        synth_log_header += "\nsv2v:\n" + sv2v_log
    synth_log = synth_log_header + "\n" + top_log_detail

    if not direct_ok or not full_json_path.is_file():
        err = top_error or "top synthesis failed"
        for mod in requested_unique:
            out[mod].error = err
            out[mod].log = synth_log
        return out

    t_augment = time.perf_counter()
    _augment_yosys_json(full_json_path, parsed_db, top_module, sv2v_line_map)
    _emit_metric_event(
        progress_cb,
        stage="batch.augment_json",
        module=top_module,
        elapsed_sec=max(0.0, time.perf_counter() - t_augment),
    )
    t_sanitize_full = time.perf_counter()
    _sanitize_netlistsvg_json_directions(full_json_path)
    _emit_metric_event(
        progress_cb,
        stage="batch.sanitize_full_json",
        module=top_module,
        elapsed_sec=max(0.0, time.perf_counter() - t_sanitize_full),
    )
    t_load_json = time.perf_counter()
    try:
        full_data = json.loads(full_json_path.read_text(encoding="utf-8"))
    except Exception as e:
        err = f"failed to parse synthesized json: {e}"
        for mod in requested_unique:
            out[mod].error = err
            out[mod].log = synth_log
        return out
    _emit_metric_event(
        progress_cb,
        stage="batch.load_full_json",
        module=top_module,
        elapsed_sec=max(0.0, time.perf_counter() - t_load_json),
        json_size=(full_json_path.stat().st_size if full_json_path.is_file() else 0),
    )
    modules = full_data.get("modules", {}) if isinstance(full_data, dict) else {}
    if not isinstance(modules, dict):
        err = "synthesized json does not contain modules"
        for mod in requested_unique:
            out[mod].error = err
            out[mod].log = synth_log
        return out

    top_module_key = _find_json_module_key(modules, top_module)
    request_to_key: Dict[str, str] = {}
    if request_mode == "instance":
        if not top_module_key:
            err = "top module not found in synthesized json"
            for req in requested_unique:
                out[req].error = err
                out[req].log = synth_log + "\n" + err
            return out
        for req in requested_unique:
            rel_chain = req_rel_instances.get(req, [])
            key = _resolve_json_module_key_for_instance(modules, top_module_key, rel_chain)
            if not key:
                out[req].error = "instance path not found in synthesized top netlist"
                chain_txt = ".".join(rel_chain) if rel_chain else "(top)"
                out[req].log = (
                    synth_log
                    + f"\nrequest: {req}\nrelative instance chain: {chain_txt}\n"
                    + "error: failed to resolve instance path in synthesized json"
                )
                continue
            request_to_key[req] = key
    else:
        for req in requested_unique:
            key = _find_json_module_key(modules, req)
            if not key:
                out[req].error = "module not found in synthesized top netlist"
                out[req].log = synth_log + f"\nmissing module in synthesized json: {req}"
                continue
            request_to_key[req] = key

    unique_keys: List[str] = []
    seen_keys: Set[str] = set()
    for req in requested_unique:
        key = request_to_key.get(req, "")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unique_keys.append(key)

    key_results: Dict[str, Dict[str, str]] = {}
    for key in unique_keys:
        key_t0 = time.perf_counter()
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", _canonical_module_name(key) or "module")
        mod_dir = tmpdir / safe_key
        mod_dir.mkdir(parents=True, exist_ok=True)
        json_path = mod_dir / f"{safe_key}.json"
        svg_path = mod_dir / f"{safe_key}.svg"
        html_path = mod_dir / f"{safe_key}.html"
        t_slice = time.perf_counter()
        sliced = _slice_yosys_json_for_module(full_data, key)
        _emit_metric_event(
            progress_cb,
            stage="batch.slice_json",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - t_slice),
        )
        t_write = time.perf_counter()
        json_path.write_text(json.dumps(sliced, indent=2), encoding="utf-8")
        _emit_metric_event(
            progress_cb,
            stage="batch.write_json",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - t_write),
        )
        t_sanitize = time.perf_counter()
        _sanitize_netlistsvg_json_directions(json_path)
        _emit_metric_event(
            progress_cb,
            stage="batch.sanitize_json",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - t_sanitize),
        )
        t_netlistsvg = time.perf_counter()
        ok, svg_log, svg_err = _run_netlistsvg_from_json(
            json_path=json_path,
            svg_path=svg_path,
            netlistsvg_cmd=netlistsvg_cmd,
            netlistsvg_dir=netlistsvg_dir,
            timeout_sec=timeout_sec,
            progress_cb=progress_cb,
            heartbeat_sec=heartbeat_sec,
            progress_meta={
                "tool": "netlistsvg",
                "module": key,
                "stage": "batch.netlistsvg",
            },
        )
        _emit_metric_event(
            progress_cb,
            stage="batch.netlistsvg_total",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - t_netlistsvg),
        )
        if not ok or not svg_path.is_file():
            key_results[key] = {
                "error": svg_err or "netlistsvg failed",
                "json_path": str(json_path),
                "svg_path": str(svg_path),
                "html_path": str(html_path),
                "render_log": svg_log + "\n" + top_log_detail,
            }
            _emit_metric_event(
                progress_cb,
                stage="batch.module_total",
                module=key,
                elapsed_sec=max(0.0, time.perf_counter() - key_t0),
                result="fail",
            )
            continue
        t_build_html = time.perf_counter()
        svg_text = svg_path.read_text(encoding="utf-8", errors="ignore")
        svg_text = _inject_svg_data_src_from_json(svg_text, json_path, key)
        svg_text = _inline_svg_styles_for_qt(svg_text)
        svg_path.write_text(svg_text, encoding="utf-8")
        _build_html(svg_text, abs_files, html_path)
        _emit_metric_event(
            progress_cb,
            stage="batch.build_html",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - t_build_html),
        )
        key_results[key] = {
            "error": "",
            "json_path": str(json_path),
            "svg_path": str(svg_path),
            "html_path": str(html_path),
            "render_log": svg_log,
        }
        _emit_metric_event(
            progress_cb,
            stage="batch.module_total",
            module=key,
            elapsed_sec=max(0.0, time.perf_counter() - key_t0),
            result="ok",
        )

    for req in requested_unique:
        req_t0 = time.perf_counter()
        key = request_to_key.get(req, "")
        if not key:
            _emit_metric_event(
                progress_cb,
                stage="batch.request_total",
                module=req,
                elapsed_sec=max(0.0, time.perf_counter() - req_t0),
                result="fail",
            )
            continue
        kr = key_results.get(key)
        if not isinstance(kr, dict):
            out[req].error = "internal error: missing render result"
            out[req].log = synth_log + f"\nrequest: {req}\njson module key: {key}\nerror: missing render result"
            _emit_metric_event(
                progress_cb,
                stage="batch.request_total",
                module=req,
                elapsed_sec=max(0.0, time.perf_counter() - req_t0),
                result="fail",
            )
            continue
        out[req].json_path = str(kr.get("json_path", ""))
        out[req].svg_path = str(kr.get("svg_path", ""))
        out[req].html_path = str(kr.get("html_path", ""))
        out[req].error = str(kr.get("error", "") or "")
        if request_mode == "instance":
            rel_chain = req_rel_instances.get(req, [])
            chain_txt = ".".join(rel_chain) if rel_chain else "(top)"
            out[req].log = (
                synth_log_header
                + f"\nrequest: {req}\nrelative instance chain: {chain_txt}\njson module key: {key}\n"
                + str(kr.get("render_log", ""))
            )
        else:
            out[req].log = (
                synth_log_header
                + f"\nmodule: {req}\njson module key: {key}\n"
                + str(kr.get("render_log", ""))
            )
        _emit_metric_event(
            progress_cb,
            stage="batch.request_total",
            module=req,
            elapsed_sec=max(0.0, time.perf_counter() - req_t0),
            result="ok" if not out[req].error else "fail",
        )
    _emit_metric_event(
        progress_cb,
        stage="batch.total",
        module=top_module,
        elapsed_sec=max(0.0, time.perf_counter() - batch_t0),
        targets=len(requested_unique),
    )
    return out
