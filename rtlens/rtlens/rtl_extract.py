from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .model import DesignDB, SourceLoc


_ID_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_$]*)\b")
_DOTTED_ID_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)+)\b")
_SIZED_LITERAL_RE = re.compile(r"\b\d[\d_]*'[sS]?[bodhBODH][0-9a-fA-F_xXzZ?]+\b")
_UNSIZED_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_$])'[01xXzZ](?![A-Za-z0-9_$])")
_NUMBER_RE = re.compile(r"\b\d[\d_]*\b")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_KEYWORDS = {
    "module",
    "endmodule",
    "input",
    "output",
    "inout",
    "wire",
    "logic",
    "reg",
    "assign",
    "always",
    "always_ff",
    "always_comb",
    "always_latch",
    "if",
    "else",
    "begin",
    "end",
    "posedge",
    "negedge",
    "or",
    "and",
    "not",
    "xor",
    "xnor",
    "case",
    "endcase",
    "for",
    "while",
    "do",
    "return",
    "function",
    "endfunction",
    "task",
    "endtask",
    "default",
    "unique",
    "priority",
    "inside",
    "foreach",
    "localparam",
    "parameter",
}
_PARAM_LIKE_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SIMPLE_SIGNAL_EXPR_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?)(?:\s*\[[^\]]+\])?\s*$"
)
_CALLABLE_DECL_SKIP_TOKENS = {
    "automatic",
    "signed",
    "unsigned",
    "static",
    "virtual",
    "pure",
    "const",
    "extern",
    "local",
    "protected",
    "rand",
    "randc",
    "input",
    "output",
    "inout",
    "ref",
    "var",
    "logic",
    "reg",
    "wire",
    "bit",
    "int",
    "integer",
    "longint",
    "shortint",
    "byte",
    "time",
    "real",
    "realtime",
    "shortreal",
    "void",
    "function",
    "task",
}


def _src(file: str, line_start: int, line_end: Optional[int] = None) -> SourceLoc:
    return SourceLoc(file=file, line=line_start if line_end is None else line_start)


@dataclass
class ExtractedSignal:
    """A signal or variable declared inside a module (wire, reg, logic, etc.)."""

    id: str
    name: str
    kind: str          # e.g. "wire", "reg", "logic", "parameter"
    width: Optional[int]
    declared_in: str   # hierarchy path of the enclosing module
    source: SourceLoc
    tags: List[str] = field(default_factory=list)


@dataclass
class ExtractedModulePort:
    """A port declared in the module header (input / output / inout)."""

    id: str
    name: str
    direction: str      # "input", "output", or "inout"
    signal_ids: List[str]
    source: SourceLoc


@dataclass
class ExtractedInstancePort:
    name: str
    direction: str
    signal_ids: List[str]
    dangling_kind: str = ""
    expr: str = ""


@dataclass
class ExtractedInstance:
    """A module instantiation inside the current module."""

    id: str
    name: str
    label: str
    module_name: str
    source: SourceLoc
    parameters: Dict[str, str] = field(default_factory=dict)
    parameter_positional: List[str] = field(default_factory=list)
    ports: List[ExtractedInstancePort] = field(default_factory=list)


@dataclass
class ExtractedAssign:
    """A continuous assignment statement (assign lhs = rhs)."""

    id: str
    name: str
    label: str
    input_signals: List[str]   # signal IDs read by the RHS expression
    output_signals: List[str]  # signal IDs driven by the LHS
    expr_summary: str
    source: SourceLoc


@dataclass
class ExtractedAlways:
    """An always / always_ff / always_comb / always_latch block."""

    id: str
    name: str
    label: str
    always_kind: str           # "always", "always_ff", "always_comb", "always_latch"
    input_signals: List[str]
    output_signals: List[str]
    clock_signals: List[str]
    reset_signals: List[str]
    stmt_summary: List[str]
    source: SourceLoc


@dataclass
class ExtractedCallable:
    """A function or task definition found inside the module."""

    id: str
    name: str
    label: str
    callable_kind: str   # "function" or "task"
    callable_key: str    # globally unique key used in DesignDB.callable_* indexes
    input_signals: List[str]
    output_signals: List[str]
    stmt_summary: str
    source: SourceLoc


@dataclass
class ExtractedModuleStructure:
    """Complete structural extraction of one elaborated module instance.

    Produced by extract_module_structure(). Contains all signals, ports,
    sub-instances, assign statements, always blocks, and callables visible
    within the module at the given hierarchy path.
    """

    module_name: str
    module_file: str
    line_start: int
    line_end: int
    hier_path: str
    signals: List[ExtractedSignal] = field(default_factory=list)
    module_ports: List[ExtractedModulePort] = field(default_factory=list)
    instances: List[ExtractedInstance] = field(default_factory=list)
    assigns: List[ExtractedAssign] = field(default_factory=list)
    always_blocks: List[ExtractedAlways] = field(default_factory=list)
    callables: List[ExtractedCallable] = field(default_factory=list)
    debug: Dict[str, object] = field(default_factory=dict)


def _signal_id(name: str) -> str:
    return f"sig_{name}"


def _instance_id(name: str) -> str:
    return f"node_inst_{name}"


def _module_port_id(name: str) -> str:
    return f"node_port_{name}"


def _tags_for_signal(name: str, kind: str, port_dir: str = "") -> List[str]:
    tags: List[str] = []
    if port_dir in {"input", "output", "inout"}:
        tags.append(port_dir)
    else:
        tags.append("internal")
    lname = name.lower()
    if "clk" in lname or "clock" in lname:
        tags.append("clock")
    if "rst" in lname or "reset" in lname:
        tags.append("reset")
    if "[" in kind:
        tags.append("bus")
    return tags


def _width_from_kind(kind: str) -> Optional[int]:
    m = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", kind)
    if not m:
        return None
    msb = int(m.group(1))
    lsb = int(m.group(2))
    return abs(msb - lsb) + 1


def _ids_in_expr(expr: str) -> List[str]:
    expr = _STRING_RE.sub(" ", expr)
    expr = _SIZED_LITERAL_RE.sub(" ", expr)
    expr = _UNSIZED_LITERAL_RE.sub(" ", expr)
    expr = _NUMBER_RE.sub(" ", expr)
    out: List[str] = []
    dotted = _DOTTED_ID_RE.findall(expr)
    for token in dotted:
        out.append(token)
    expr = _DOTTED_ID_RE.sub(" ", expr)
    for token in _ID_RE.findall(expr):
        if token in _KEYWORDS:
            continue
        if token.isdigit():
            continue
        out.append(token)
    return out


def _base_signal_name(name: str) -> str:
    return name.split(".", 1)[0]


def _suppress_base_when_member_present(names: List[str]) -> List[str]:
    member_bases = {_base_signal_name(name) for name in names if "." in name}
    if not member_bases:
        return names
    return [name for name in names if name not in member_bases]


def _normalize_block_signal_groups(groups: List[List[str]]) -> List[List[str]]:
    member_bases = {
        _base_signal_name(name)
        for group in groups
        for name in group
        if "." in name
    }
    if not member_bases:
        return groups
    return [[name for name in group if name not in member_bases] for group in groups]


def _looks_like_param_or_macro(token: str) -> bool:
    return bool(_PARAM_LIKE_RE.fullmatch(token))


def _uniq_keep_order(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _is_simple_signal_expr(expr: str, parsed_ids: List[str]) -> bool:
    if len(parsed_ids) != 1:
        return False
    m = _SIMPLE_SIGNAL_EXPR_RE.match(expr or "")
    if not m:
        return False
    return m.group(1) == parsed_ids[0]


def _sanitize_name_for_id(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_$]+", "_", name).strip("_")
    return cleaned or "expr"


def _short_callable_name(key: str, fallback: str = "") -> str:
    raw = key.split(":", 1)[-1]
    tail = raw.split("::")[-1]
    tail = tail.split(".")[-1]
    return tail or fallback or raw


def _load_file_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except Exception:
        return []


def _line_text(lines: List[str], line: int) -> str:
    if line <= 0 or line > len(lines):
        return ""
    return lines[line - 1]


def _find_callable_pos(text: str, names: List[str]) -> int:
    best = -1
    for name in names:
        if not name:
            continue
        m = re.search(rf"\b{re.escape(name)}\s*\(", text)
        if not m:
            continue
        if best < 0 or m.start() < best:
            best = m.start()
    return best


def _extract_assignment_lhs(text: str, call_pos: int) -> str:
    if call_pos < 0:
        return ""
    prefix = text[:call_pos]
    eq = prefix.rfind("=")
    if eq < 0:
        return ""
    if eq > 0 and prefix[eq - 1] in {"=", "!"}:
        return ""
    if eq + 1 < len(prefix) and prefix[eq + 1] == "=":
        return ""
    lhs = prefix[:eq].strip()
    if lhs.startswith("if ") or lhs.startswith("if("):
        return ""
    if lhs.startswith("for ") or lhs.startswith("for("):
        return ""
    if lhs.startswith("while ") or lhs.startswith("while("):
        return ""
    return lhs


def _strip_line_comment(text: str) -> str:
    return text.split("//", 1)[0]


def _extract_callable_decl_name(rest: str) -> str:
    head = rest.split("(", 1)[0]
    head = head.split(";", 1)[0]
    head = re.sub(r"\[[^\]]*\]", " ", head)
    toks = [
        tok
        for tok in _ID_RE.findall(head)
        if tok not in _CALLABLE_DECL_SKIP_TOKENS
    ]
    return toks[-1] if toks else ""


def _collect_local_callable_defs(lines: List[str], line_start: int, line_end: int) -> List[Tuple[str, str, int]]:
    out: List[Tuple[str, str, int]] = []
    if not lines:
        return out
    start = max(1, line_start)
    end = min(len(lines), max(line_end, line_start))
    for ln in range(start, end + 1):
        raw = _strip_line_comment(_line_text(lines, ln))
        m = re.match(r"^\s*(function|task)\b(.*)$", raw)
        if not m:
            continue
        kind = m.group(1)
        name = _extract_callable_decl_name(m.group(2))
        if not name:
            continue
        out.append((kind, name, ln))
    return out


def _extract_call_arg_text(text: str, call_pos: int) -> str:
    if call_pos < 0:
        return ""
    p = text.find("(", call_pos)
    if p < 0:
        return ""
    depth = 0
    for i in range(p, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[p + 1 : i]
    return text[p + 1 :]


def _resolve_callable_key(
    design: DesignDB,
    *,
    kind: str,
    name: str,
    module_name: str,
    module_file_abs: str,
    module_line_start: int,
    module_line_end: int,
) -> str:
    candidates = sorted(design.callable_name_index.get(name, set()))
    fallback = ""
    for key in candidates:
        if design.callable_kinds.get(key, "") != kind:
            continue
        loc = design.callable_defs.get(key)
        if loc and os.path.abspath(loc.file) == module_file_abs and module_line_start <= loc.line <= module_line_end:
            return key
        if not fallback:
            fallback = key
    if fallback:
        return fallback
    return f"{kind}:{module_name}.{name}"


def extract_module_structure(design: DesignDB, hier_path: str) -> ExtractedModuleStructure:
    """Extract the full structural view of a module instance from an elaborated design.

    Args:
        design:    Elaborated DesignDB produced by slang_backend.
        hier_path: Dot-separated hierarchy path of the instance to extract
                   (e.g. "top.u_core.u_alu").

    Returns:
        ExtractedModuleStructure with all signals, ports, sub-instances,
        assign/always blocks, and callables populated.

    Raises:
        KeyError: If hier_path or the corresponding module name is not found in design.
    """
    if hier_path not in design.hier:
        raise KeyError(f"hier path not found: {hier_path}")

    hier_node = design.hier[hier_path]
    module = design.modules.get(hier_node.module_name)
    if module is None:
        raise KeyError(f"module not found for hier path: {hier_path}")

    extracted = ExtractedModuleStructure(
        module_name=module.name,
        module_file=module.file,
        line_start=module.start_line,
        line_end=module.end_line,
        hier_path=hier_path,
    )
    declared_signal_names: Set[str] = set()
    active_child_names: Set[str] = set()
    for child_path in hier_node.children:
        child = design.hier.get(child_path)
        if child and child.inst_name:
            active_child_names.add(child.inst_name)

    seen_signals: Set[str] = set()
    for port_name, port in module.ports.items():
        sig_id = _signal_id(port_name)
        sig_kind = module.signals.get(port_name).kind if port_name in module.signals else "port"
        extracted.signals.append(
            ExtractedSignal(
                id=sig_id,
                name=port_name,
                kind=sig_kind,
                width=_width_from_kind(sig_kind),
                declared_in=module.name,
                source=SourceLoc(file=module.file, line=port.line),
                tags=_tags_for_signal(port_name, sig_kind, port.direction),
            )
        )
        extracted.module_ports.append(
            ExtractedModulePort(
                id=_module_port_id(port_name),
                name=port_name,
                direction=port.direction,
                signal_ids=[sig_id],
                source=SourceLoc(file=module.file, line=port.line),
            )
        )
        seen_signals.add(port_name)
        declared_signal_names.add(port_name)

    for sig_name, sig in module.signals.items():
        if sig_name in seen_signals:
            continue
        extracted.signals.append(
            ExtractedSignal(
                id=_signal_id(sig_name),
                name=sig_name,
                kind=sig.kind,
                width=_width_from_kind(sig.kind),
                declared_in=module.name,
                source=SourceLoc(file=module.file, line=sig.line),
                tags=_tags_for_signal(sig_name, sig.kind),
            )
        )
        declared_signal_names.add(sig_name)

    synthetic_member_names: Set[str] = set()
    synthetic_undeclared_names: Set[str] = set()

    def _collect_member_refs(tokens: List[str]) -> None:
        for tok in tokens:
            if "." not in tok:
                if tok not in declared_signal_names:
                    if _looks_like_param_or_macro(tok):
                        continue
                    synthetic_undeclared_names.add(tok)
                continue
            base = _base_signal_name(tok)
            if base in declared_signal_names:
                synthetic_member_names.add(tok)
            else:
                synthetic_undeclared_names.add(tok)

    for assign in module.assignments:
        _collect_member_refs(assign.lhs)
    for block in getattr(module, "always_blocks", []):
        _collect_member_refs(block.writes)
        _collect_member_refs(block.clock_signals)
        _collect_member_refs(block.reset_signals)
    for inst in module.instances:
        for expr in inst.connections.values():
            _collect_member_refs(_ids_in_expr(expr))
        for expr in getattr(inst, "positional", []):
            _collect_member_refs(_ids_in_expr(expr))

    allowed_signal_names: Set[str] = set(declared_signal_names)
    for member_name in sorted(synthetic_member_names):
        base = _base_signal_name(member_name)
        base_sig = module.signals.get(base)
        base_port = module.ports.get(base)
        source_line = 1
        base_kind = "logic"
        port_dir = ""
        if base_sig:
            source_line = base_sig.line
            base_kind = base_sig.kind
        if base_port:
            source_line = base_port.line
            port_dir = base_port.direction
        extracted.signals.append(
            ExtractedSignal(
                id=_signal_id(member_name),
                name=member_name,
                kind=f"{base_kind}.member",
                width=None,
                declared_in=module.name,
                source=SourceLoc(file=module.file, line=source_line),
                tags=_tags_for_signal(member_name, base_kind, port_dir),
            )
        )
        allowed_signal_names.add(member_name)

    for sig_name in sorted(synthetic_undeclared_names):
        if sig_name in allowed_signal_names:
            continue
        extracted.signals.append(
            ExtractedSignal(
                id=_signal_id(sig_name),
                name=sig_name,
                kind="synthetic",
                width=None,
                declared_in=module.name,
                source=SourceLoc(file=module.file, line=max(module.start_line, 1)),
                tags=["internal", "synthetic"],
            )
        )
        allowed_signal_names.add(sig_name)

    expr_assigns: List[ExtractedAssign] = []
    expr_assign_index = 0
    expr_signal_names: Set[str] = set()

    def _alloc_expr_signal_name(inst_name: str, port_name: str) -> str:
        base = _sanitize_name_for_id(f"__expr_{inst_name}_{port_name}")
        cand = base
        i = 2
        while cand in allowed_signal_names or cand in expr_signal_names:
            cand = f"{base}_{i}"
            i += 1
        expr_signal_names.add(cand)
        return cand

    def _build_instance_port(
        inst_name: str,
        port_name: str,
        direction: str,
        expr: str,
        source_line: int,
    ) -> ExtractedInstancePort:
        nonlocal expr_assign_index
        expr_str = (expr or "").strip()
        raw_ids = _ids_in_expr(expr_str)
        parsed_ids = [sig for sig in raw_ids if sig in allowed_signal_names]
        parsed_ids = _uniq_keep_order(parsed_ids)
        signal_ids = [_signal_id(sig) for sig in parsed_ids]
        dangling_kind = ""
        expr_node_needed = (
            direction in {"input", "inout", "unknown"}
            and bool(expr_str)
            and bool(parsed_ids)
            and not _is_simple_signal_expr(expr_str, parsed_ids)
        )
        if expr_node_needed:
            out_name = _alloc_expr_signal_name(inst_name, port_name)
            out_sig_id = _signal_id(out_name)
            extracted.signals.append(
                ExtractedSignal(
                    id=out_sig_id,
                    name=out_name,
                    kind="expr",
                    width=None,
                    declared_in=module.name,
                    source=SourceLoc(file=module.file, line=source_line),
                    tags=["internal", "synthetic"],
                )
            )
            allowed_signal_names.add(out_name)
            expr_assign_index += 1
            expr_assigns.append(
                ExtractedAssign(
                    id=f"node_assign_expr_{expr_assign_index}",
                    name=f"expr_{inst_name}_{port_name}",
                    label=f"expr_{inst_name}.{port_name}",
                    input_signals=signal_ids,
                    output_signals=[out_sig_id],
                    expr_summary=expr_str,
                    source=SourceLoc(file=module.file, line=source_line),
                )
            )
            signal_ids = [out_sig_id]
        elif expr_str and not signal_ids and direction in {"input", "inout", "unknown"}:
            dangling_kind = "const_expr_input"
        return ExtractedInstancePort(
            name=port_name,
            direction=direction,
            signal_ids=signal_ids,
            dangling_kind=dangling_kind,
            expr=expr_str,
        )

    for inst in module.instances:
        if active_child_names and inst.name not in active_child_names:
            continue
        child_mod = design.modules.get(inst.module_type)
        port_items: List[ExtractedInstancePort] = []
        if child_mod and child_mod.ports:
            positional = list(getattr(inst, "positional", []))
            for idx, (port_name, port_def) in enumerate(child_mod.ports.items()):
                expr = inst.connections.get(port_name, "")
                if not expr and idx < len(positional):
                    expr = positional[idx]
                port_items.append(
                    _build_instance_port(
                        inst_name=inst.name,
                        port_name=port_name,
                        direction=port_def.direction,
                        expr=expr,
                        source_line=inst.line,
                    )
                )
        else:
            for port_name, expr in sorted(inst.connections.items()):
                port_items.append(
                    _build_instance_port(
                        inst_name=inst.name,
                        port_name=port_name,
                        direction="unknown",
                        expr=expr,
                        source_line=inst.line,
                    )
                )
        extracted.instances.append(
            ExtractedInstance(
                id=_instance_id(inst.name),
                name=inst.name,
                label=f"{inst.name} : {inst.module_type}",
                module_name=inst.module_type,
                source=SourceLoc(file=module.file, line=inst.line),
                parameters=dict(inst.parameters),
                parameter_positional=list(getattr(inst, "parameter_positional", [])),
                ports=port_items,
            )
        )

    extracted.assigns.extend(expr_assigns)

    for idx, assign in enumerate(module.assignments, 1):
        assign_inputs = [sig for sig in assign.rhs if sig in allowed_signal_names]
        assign_outputs = [sig for sig in assign.lhs if sig in allowed_signal_names]
        assign_inputs, assign_outputs = _normalize_block_signal_groups([assign_inputs, assign_outputs])
        extracted.assigns.append(
            ExtractedAssign(
                id=f"node_assign_{idx}",
                name=f"assign_{idx}",
                label=f"assign_{assign.lhs[0] if assign.lhs else idx}",
                input_signals=[_signal_id(sig) for sig in assign_inputs],
                output_signals=[_signal_id(sig) for sig in assign_outputs],
                expr_summary=assign.text or "assign",
                source=SourceLoc(file=module.file, line=assign.line),
            )
        )

    for idx, block in enumerate(getattr(module, "always_blocks", []), 1):
        block_inputs = [sig for sig in block.reads if sig in allowed_signal_names]
        block_outputs = [sig for sig in block.writes if sig in allowed_signal_names]
        block_clocks = [sig for sig in block.clock_signals if sig in allowed_signal_names]
        block_resets = [sig for sig in block.reset_signals if sig in allowed_signal_names]
        block_inputs, block_outputs, block_clocks, block_resets = _normalize_block_signal_groups(
            [block_inputs, block_outputs, block_clocks, block_resets]
        )
        extracted.always_blocks.append(
            ExtractedAlways(
                id=f"node_always_{idx}",
                name=f"always_{idx}",
                label=f"{block.kind} {idx}",
                always_kind=block.kind,
                input_signals=[_signal_id(sig) for sig in block_inputs],
                output_signals=[_signal_id(sig) for sig in block_outputs],
                clock_signals=[_signal_id(sig) for sig in block_clocks],
                reset_signals=[_signal_id(sig) for sig in block_resets],
                stmt_summary=list(block.stmt_summary),
                source=SourceLoc(file=module.file, line=block.line_start),
            )
        )

    module_file_abs = os.path.abspath(module.file)
    source_lines = _load_file_lines(module.file)
    callable_seen: Set[Tuple[int, str]] = set()
    callable_index = 0
    for (site_file, site_line, token), keys in sorted(
        design.callable_ref_sites.items(),
        key=lambda item: (item[0][0], item[0][1], item[0][2]),
    ):
        if not site_file:
            continue
        if os.path.abspath(site_file) != module_file_abs:
            continue
        if site_line < module.start_line or site_line > module.end_line:
            continue
        valid_keys = [k for k in keys if design.callable_kinds.get(k, "") in {"function", "task"}]
        if not valid_keys:
            continue
        raw_text = _line_text(source_lines, site_line)
        text = raw_text.strip()
        if not text:
            text = f"callable_ref:{token}"
        for key in valid_keys:
            dedup = (site_line, key)
            if dedup in callable_seen:
                continue
            callable_seen.add(dedup)

            kind = design.callable_kinds.get(key, "function")
            ref_name = design.callable_names.get(key, _short_callable_name(key, token))
            short_name = _short_callable_name(key, ref_name)

            call_pos = _find_callable_pos(raw_text, [token, ref_name, short_name])
            args_text = _extract_call_arg_text(raw_text, call_pos)
            in_names = _uniq_keep_order([sig for sig in _ids_in_expr(args_text) if sig in allowed_signal_names])
            lhs_text = _extract_assignment_lhs(raw_text, call_pos)
            out_names = _uniq_keep_order([sig for sig in _ids_in_expr(lhs_text) if sig in allowed_signal_names])

            in_names, out_names = _normalize_block_signal_groups([in_names, out_names])
            out_set = set(out_names)
            in_names = [sig for sig in in_names if sig not in out_set]

            callable_index += 1
            extracted.callables.append(
                ExtractedCallable(
                    id=f"node_callable_{callable_index}",
                    name=f"{kind}_{_sanitize_name_for_id(short_name)}_{callable_index}",
                    label=f"{kind} {short_name}",
                    callable_kind=kind,
                    callable_key=key,
                    input_signals=[_signal_id(sig) for sig in in_names],
                    output_signals=[_signal_id(sig) for sig in out_names],
                    stmt_summary=text,
                    source=SourceLoc(file=site_file, line=site_line),
                )
            )

    local_callable_defs = _collect_local_callable_defs(source_lines, module.start_line, module.end_line)
    if local_callable_defs:
        active_lines: Set[int] = set()
        for assign in module.assignments:
            if module.start_line <= assign.line <= module.end_line:
                active_lines.add(assign.line)
        for block in getattr(module, "always_blocks", []):
            b_start = max(module.start_line, block.line_start)
            b_end = min(module.end_line, max(block.line_end, block.line_start))
            for ln in range(b_start, b_end + 1):
                active_lines.add(ln)

        for site_line in sorted(active_lines):
            raw_text = _line_text(source_lines, site_line)
            text = _strip_line_comment(raw_text).strip()
            if not text:
                continue
            if re.match(r"^\s*(function|task|endfunction|endtask)\b", text):
                continue
            for kind, name, _def_line in local_callable_defs:
                for match in re.finditer(rf"\b{re.escape(name)}\s*\(", text):
                    key = _resolve_callable_key(
                        design,
                        kind=kind,
                        name=name,
                        module_name=module.name,
                        module_file_abs=module_file_abs,
                        module_line_start=module.start_line,
                        module_line_end=module.end_line,
                    )
                    dedup = (site_line, key)
                    if dedup in callable_seen:
                        continue
                    callable_seen.add(dedup)

                    args_text = _extract_call_arg_text(text, match.start())
                    in_names = _uniq_keep_order([sig for sig in _ids_in_expr(args_text or text) if sig in allowed_signal_names])
                    lhs_text = _extract_assignment_lhs(text, match.start())
                    out_names = _uniq_keep_order([sig for sig in _ids_in_expr(lhs_text) if sig in allowed_signal_names])

                    in_names, out_names = _normalize_block_signal_groups([in_names, out_names])
                    out_set = set(out_names)
                    in_names = [sig for sig in in_names if sig not in out_set]

                    callable_index += 1
                    extracted.callables.append(
                        ExtractedCallable(
                            id=f"node_callable_{callable_index}",
                            name=f"{kind}_{_sanitize_name_for_id(name)}_{callable_index}",
                            label=f"{kind} {name}",
                            callable_kind=kind,
                            callable_key=key,
                            input_signals=[_signal_id(sig) for sig in in_names],
                            output_signals=[_signal_id(sig) for sig in out_names],
                            stmt_summary=text,
                            source=SourceLoc(file=module.file, line=site_line),
                        )
                    )

    extracted.debug = {
        "declared_signal_count": len(declared_signal_names),
        "synthetic_member_count": len(synthetic_member_names),
        "synthetic_member_names": sorted(synthetic_member_names),
        "synthetic_undeclared_count": len(synthetic_undeclared_names),
        "synthetic_undeclared_names": sorted(synthetic_undeclared_names),
        "allowed_signal_count": len(allowed_signal_names),
        "callable_count": len(extracted.callables),
    }
    return extracted
