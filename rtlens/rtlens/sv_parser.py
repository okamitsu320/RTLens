"""Lightweight, regex-based SystemVerilog / Verilog parser.

Parses ``.sv`` / ``.v`` source files without requiring any external binary,
producing a :class:`~rtlens.model.DesignDB` that records module definitions,
port/signal declarations, continuous-assign statements, always blocks, and
module instances.

Supported constructs
--------------------
* ``module`` / ``endmodule`` boundaries (ANSI and non-ANSI port styles)
* ``input`` / ``output`` / ``inout`` port declarations
* ``wire`` / ``logic`` / ``reg`` and arbitrary user-defined-type signals
* ``assign`` statements (LHS and RHS identifier extraction)
* ``always`` / ``always_ff`` / ``always_comb`` / ``always_latch`` / ``initial``
  blocks with write/read tracking and clock/reset detection
* Named and positional module instantiations with parameter overrides
* backtick ``ifdef`` / ``ifndef`` / ``elsif`` / ``else`` / ``endif``
  conditional-compilation directives
* ``(* … *)`` SystemVerilog attribute blocks (stripped before parsing)
* ``function`` / ``task`` scope skipping (their local declarations are ignored)

Known limitations
-----------------
* No macro expansion beyond the ``ifdef`` family — backtick ``define`` /
  ``include`` are not processed.
* Expression widths are **not** evaluated; ``[WIDTH-1:0]`` is recorded as a
  literal string.
* ``generate`` / ``genvar`` constructs are parsed on a best-effort basis and
  generated instances may not be extracted.
* The fallback parser is intentionally simple; use the slang backend
  (:mod:`rtlens.slang_backend`) for full IEEE-1800 elaboration.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Dict, Iterable, List, Optional, Set

from .model import AlwaysBlock, Assignment, DesignDB, InstanceDef, ModuleDef, PortDef, SignalDef

MODULE_RE = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)")
MODULE_NAME_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_$]*)\s*(?:#\s*\((.*?)\))?\s*\(")
MODULE_NAME_LOOSE_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_$]*)\s*(?:#\s*\(.*)?$")
ENDMODULE_RE = re.compile(r"^\s*endmodule\b")
DECL_RE = re.compile(
    r"^\s*(input|output|inout|wire|logic|reg)\b(?:\s+(?:signed|unsigned))?(?:\s*\[[^\]]+\])?\s+(.+?);"
)
TYPE_DECL_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_$:]*)\b(?:\s+(?:signed|unsigned))?(?:\s*\[[^\]]+\])?\s+(.+?);"
)
HEADER_PORT_RE = re.compile(
    r"^\s*(input|output|inout)\b(?:\s+(?:wire|logic|reg))?(?:\s+(?:signed|unsigned))?(?:\s*\[[^\]]+\])?\s+(.+?)(?:,)?\s*$"
)
ASSIGN_RE = re.compile(r"^\s*assign\s+(.+?)\s*=\s*(.+?);")
INST_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_$]*)\s*(?:#\s*\((.*?)\))?\s+([a-zA-Z_][a-zA-Z0-9_$]*)\s*\((.*?)\)\s*;")
ID_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_$]*)\b")
DOTTED_ID_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)+)\b")
ALWAYS_HEAD_RE = re.compile(r"^\s*(always(?:_ff|_comb|_latch)?|initial)\b(.*)$")
SIZED_LITERAL_RE = re.compile(r"\b\d[\d_]*'[sS]?[bodhBODH][0-9a-fA-F_xXzZ?]+\b")
UNSIZED_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_$])'[01xXzZ](?![A-Za-z0-9_$])")
NUMBER_RE = re.compile(r"\b\d[\d_]*\b")
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
STMT_ASSIGN_RE = re.compile(
    r"(?:^|[;\n]\s*|\bbegin\b\s*|\belse\b\s*|:\s*)(?:if\s*\([^;\n]*\)\s*)*([A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*(?:\[[^\]]+\])?)\s*(<=|=)(?!=)",
    re.S,
)
ATTR_BLOCK_RE = re.compile(r"\(\*.*?\*\)")

KEYWORDS = {
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


def _strip_line_comment(text: str) -> str:
    """Return *text* with any trailing ``//`` line comment removed.

    Handles string literals so that ``//`` inside a quoted string is preserved.
    """
    in_string = False
    escaped = False
    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "/" and idx + 1 < len(text) and text[idx + 1] == "/":
            return text[:idx]
    return text


def _strip_sv_attributes(text: str) -> str:
    """Strip all ``(* … *)`` SystemVerilog attribute blocks from *text*."""
    prev = text
    while True:
        next_text = ATTR_BLOCK_RE.sub(" ", prev)
        if next_text == prev:
            break
        prev = next_text
    return prev


def _preprocess_lines(lines: List[str], defined_macros: Optional[Set[str]] = None) -> List[str]:
    """Apply backtick ``ifdef`` / ``ifndef`` / ``elsif`` / ``else`` / ``endif`` directives.

    Lines inside an inactive conditional branch are replaced with ``"\\n"``
    so that line numbers in the output match the original source.

    Note: backtick ``define`` / ``include`` are **not** processed.

    Args:
        lines: Raw source lines (e.g. from ``f.readlines()``).
        defined_macros: Names to treat as defined. Defaults to the empty set.

    Returns:
        A new list of lines with conditional branches filtered out.
    """
    macros = set(defined_macros or set())
    out: List[str] = []
    stack: List[Dict[str, bool]] = []

    def _current_active() -> bool:
        return all(frame["active"] for frame in stack)

    for line in lines:
        stripped = _strip_line_comment(line).strip()
        m = re.match(r"^`(ifdef|ifndef|elsif|else|endif)\b(?:\s+([A-Za-z_][A-Za-z0-9_$]*))?", stripped)
        if not m:
            out.append(line if _current_active() else "\n")
            continue

        kind = m.group(1)
        name = (m.group(2) or "").strip()
        if kind in {"ifdef", "ifndef"}:
            parent_active = _current_active()
            cond = name in macros
            if kind == "ifndef":
                cond = not cond
            active = parent_active and cond
            stack.append({"parent": parent_active, "taken": bool(cond), "active": active})
        elif kind == "elsif":
            if stack:
                frame = stack[-1]
                cond = name in macros
                active = frame["parent"] and (not frame["taken"]) and cond
                frame["active"] = active
                frame["taken"] = frame["taken"] or cond
        elif kind == "else":
            if stack:
                frame = stack[-1]
                active = frame["parent"] and (not frame["taken"])
                frame["active"] = active
                frame["taken"] = True
        elif kind == "endif":
            if stack:
                stack.pop()
        out.append("\n")
    return out


def _clean_decl_names(raw: str) -> List[str]:
    names: List[str] = []
    for piece in raw.split(","):
        name = piece.strip()
        name = re.sub(r"\s*=.*$", "", name).strip()
        name = re.sub(r"\[[^\]]+\]", "", name).strip()
        if name:
            names.append(name)
    return names


def _split_decl_items(raw: str) -> List[tuple[str, str]]:
    items: List[tuple[str, str]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" in piece:
            lhs, rhs = piece.split("=", 1)
            name = re.sub(r"\[[^\]]+\]", "", lhs).strip()
            items.append((name, rhs.strip()))
        else:
            name = re.sub(r"\[[^\]]+\]", "", piece).strip()
            items.append((name, ""))
    return items


def _parse_header_port_items(raw: str, cur_dir: str | None) -> tuple[List[tuple[str, str]], str | None]:
    out: List[tuple[str, str]] = []
    active = cur_dir
    for piece in _split_top_level_csv(raw):
        piece = piece.strip()
        if not piece:
            continue
        m = re.match(
            r"^(?:(input|output|inout)\b(?:\s+(?:wire|logic|reg))?(?:\s+(?:signed|unsigned))?(?:\s*\[[^\]]+\])?\s+)?(.+?)$",
            piece,
        )
        if not m:
            continue
        direction = m.group(1) or active
        name_raw = m.group(2).strip()
        if direction:
            active = direction
            for n in _clean_decl_names(name_raw):
                out.append((direction, n))
    return out, active


def _ids_in_expr(expr: str) -> List[str]:
    """Extract all non-keyword identifiers referenced in *expr*.

    Numeric literals, string literals, and SV keywords are stripped before
    scanning.  Dotted hierarchical names (e.g. ``u0.q``) are returned as a
    single token.

    Args:
        expr: An arbitrary SV expression string.

    Returns:
        Ordered list of identifier strings (may contain duplicates).
    """
    expr = STRING_RE.sub(" ", expr)
    expr = SIZED_LITERAL_RE.sub(" ", expr)
    expr = UNSIZED_LITERAL_RE.sub(" ", expr)
    expr = NUMBER_RE.sub(" ", expr)
    out: List[str] = []
    dotted_matches = DOTTED_ID_RE.findall(expr)
    for token in dotted_matches:
        out.append(token)
    expr = DOTTED_ID_RE.sub(" ", expr)
    for token in ID_RE.findall(expr):
        if token in KEYWORDS:
            continue
        if token.isdigit():
            continue
        out.append(token)
    return out


def _base_lhs_ids(expr: str) -> List[str]:
    out: List[str] = []
    for piece in expr.split(","):
        piece = piece.strip()
        if not piece:
            continue
        m = DOTTED_ID_RE.search(piece)
        if not m:
            m = ID_RE.search(piece)
        if m:
            tok = m.group(1)
            if tok not in KEYWORDS and not tok.isdigit():
                out.append(tok)
    return out


def _strip_proc_prefix(kind: str, text: str) -> str:
    body = text.strip()
    if kind == "initial":
        return re.sub(r"^\s*initial\b", "", body, count=1).strip()
    body = re.sub(r"^\s*always(?:_ff|_comb|_latch)?\b", "", body, count=1).strip()
    if body.startswith("@"):
        depth = 0
        for idx, ch in enumerate(body):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth <= 0:
                    body = body[idx + 1 :].strip()
                    break
    while body.startswith("#"):
        m = re.match(r"^#\s*(?:\d+|[A-Za-z_][A-Za-z0-9_$]*|\([^)]*\))\s*", body)
        if not m:
            break
        body = body[m.end() :].strip()
    return body


def _extract_clock_reset(text: str) -> tuple[List[str], List[str]]:
    """Heuristically identify clock and reset signals in an always-block.

    Signals with "clk" or "clock" in their name inside the sensitivity list are
    classified as clocks; signals with "rst" or "reset" anywhere in the block
    text are classified as resets.

    Args:
        text: Full text of the always block (including the header line).

    Returns:
        Tuple ``(clocks, resets)`` — each a deduplicated list of signal names.
    """
    clocks: List[str] = []
    resets: List[str] = []
    ev = re.search(r"@\s*\((.*?)\)", text, re.S)
    if ev:
        body = ev.group(1)
        for tok in _ids_in_expr(body):
            lname = tok.lower()
            if ("clk" in lname or "clock" in lname) and tok not in clocks:
                clocks.append(tok)
            if ("rst" in lname or "reset" in lname) and tok not in resets:
                resets.append(tok)
    for tok in _ids_in_expr(text):
        lname = tok.lower()
        if ("rst" in lname or "reset" in lname) and tok not in resets:
            resets.append(tok)
    return clocks, resets


def _split_top_level_csv(text: str) -> List[str]:
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            part = "".join(cur).strip()
            if part:
                parts.append(part)
            cur = []
            continue
        cur.append(ch)
    part = "".join(cur).strip()
    if part:
        parts.append(part)
    return parts


def _parse_named_assoc(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in _split_top_level_csv(text):
        m = re.match(r"^\.\s*([a-zA-Z_][a-zA-Z0-9_$]*)\s*\((.*)\)\s*$", part, re.S)
        if not m:
            continue
        name = m.group(1)
        expr = m.group(2).strip()
        out[name] = expr
    return out


def _parse_positional_assoc(text: str) -> List[str]:
    out: List[str] = []
    for part in _split_top_level_csv(text):
        if part.lstrip().startswith("."):
            continue
        out.append(part.strip())
    return out


def parse_sv_files(files: Iterable[str], defined_macros: Optional[Set[str]] = None) -> DesignDB:
    """Parse one or more SystemVerilog / Verilog source files into a DesignDB.

    Files are processed in order; a module defined in a later file overwrites
    a same-named module from an earlier file. Non-existent paths are silently
    skipped.

    Args:
        files: Iterable of file paths to parse (``.sv``, ``.v``, etc.).
        defined_macros: Optional set of macro names to treat as defined when
            evaluating backtick ``ifdef`` / ``ifndef`` directives. Backtick
            ``define`` directives inside the source are **not** processed.

    Returns:
        A :class:`~rtlens.model.DesignDB` populated with all modules found.
    """
    db = DesignDB()
    for file_path in files:
        if not os.path.isfile(file_path):
            continue
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = _preprocess_lines(f.readlines(), defined_macros=defined_macros)

        i = 0
        while i < len(lines):
            line = lines[i]
            m = MODULE_RE.match(line)
            param_header_open = False
            if not m and line.strip() == "module":
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines):
                    m = MODULE_NAME_RE.match(lines[j])
                    if not m:
                        m = MODULE_NAME_LOOSE_RE.match(lines[j])
                    if m:
                        param_header_open = "#(" in lines[j] and not re.search(r"\)\s*\(", lines[j])
                        i = j
            if not m:
                i += 1
                continue

            mod_name = m.group(1)
            mod = ModuleDef(name=mod_name, file=file_path, start_line=i + 1, end_line=i + 1)
            decl_line = lines[i]
            i += 1
            stmt = ""
            stmt_line = i + 1
            in_header_ports = not param_header_open
            func_task_depth = 0
            if not param_header_open and ";" in _strip_line_comment(decl_line) and "(" not in _strip_line_comment(decl_line):
                in_header_ports = False
            header_dir: str | None = None
            while i < len(lines):
                cur = lines[i]
                if ENDMODULE_RE.match(cur):
                    mod.end_line = i + 1
                    break

                if param_header_open:
                    if re.search(r"\)\s*\(", cur):
                        param_header_open = False
                        in_header_ports = True
                    i += 1
                    continue

                cur_no_comment = _strip_sv_attributes(_strip_line_comment(cur)).strip()
                # Function/task local declarations (e.g. "input in_data;") must not
                # be treated as module ports/signals. Skip parsing while inside these
                # scopes.
                if func_task_depth > 0:
                    if re.match(r"^\s*end(function|task)\b", cur_no_comment):
                        func_task_depth = max(0, func_task_depth - 1)
                    elif re.match(r"^\s*(function|task)\b", cur_no_comment):
                        func_task_depth += 1
                    i += 1
                    continue
                if re.match(r"^\s*(function|task)\b", cur_no_comment):
                    func_task_depth = 1
                    i += 1
                    continue

                if in_header_ports:
                    if cur.strip() == ");":
                        in_header_ports = False
                        i += 1
                        continue
                    header_line = _strip_sv_attributes(_strip_line_comment(cur)).strip().rstrip(",")
                    hp = HEADER_PORT_RE.match(header_line)
                    if hp:
                        items, header_dir = _parse_header_port_items(header_line, header_dir)
                        for kind, n in items:
                            mod.ports[n] = PortDef(name=n, direction=kind, line=i + 1)
                            mod.signals[n] = SignalDef(name=n, line=i + 1, kind=kind)
                    if ");" in cur:
                        in_header_ports = False
                    i += 1
                    continue

                am = ALWAYS_HEAD_RE.match(cur)
                if am:
                    kind = am.group(1)
                    start_line = i + 1
                    block_lines = [_strip_line_comment(cur).rstrip()]
                    begin_depth = 1 if re.search(r"\bbegin\b", cur) else 0
                    single_stmt = begin_depth == 0 and block_lines[0].strip().endswith(";")
                    if not single_stmt:
                        i += 1
                        while i < len(lines):
                            nxt = _strip_line_comment(lines[i]).rstrip()
                            block_lines.append(nxt)
                            begin_depth += len(re.findall(r"\bbegin\b", nxt))
                            begin_depth -= len(re.findall(r"\bend\b", nxt))
                            if begin_depth <= 0 and (nxt.strip().endswith(";") or re.search(r"\bend\b", nxt)):
                                break
                            i += 1
                    block_text = "\n".join(block_lines)
                    parse_text = block_text if not single_stmt else _strip_proc_prefix(kind, block_lines[0])
                    writes: List[str] = []
                    for m_assign in STMT_ASSIGN_RE.finditer(parse_text):
                        lhs_ids = _base_lhs_ids(m_assign.group(1))
                        for tok in lhs_ids:
                            if tok not in writes:
                                writes.append(tok)
                    reads = [tok for tok in _ids_in_expr(parse_text) if tok not in writes]
                    dedup_reads: List[str] = []
                    for tok in reads:
                        if tok not in dedup_reads:
                            dedup_reads.append(tok)
                    clocks, resets = _extract_clock_reset(block_text)
                    stmt_summary = [ln.strip() for ln in block_lines if ln.strip()][:4]
                    mod.always_blocks.append(
                        AlwaysBlock(
                            kind=kind,
                            line_start=start_line,
                            line_end=i + 1,
                            reads=dedup_reads,
                            writes=writes,
                            clock_signals=clocks,
                            reset_signals=resets,
                            stmt_summary=stmt_summary,
                        )
                    )
                    i += 1
                    continue

                no_comment = _strip_sv_attributes(_strip_line_comment(cur)).strip()
                if no_comment:
                    if not stmt:
                        stmt_line = i + 1
                    stmt = f"{stmt} {no_comment}".strip()

                if ";" in no_comment:
                    for chunk in stmt.split(";"):
                        one = chunk.strip()
                        if not one:
                            continue
                        one = _strip_sv_attributes(f"{one};").strip()
                        if one == ";":
                            continue

                        decl = DECL_RE.match(one)
                        if decl:
                            kind = decl.group(1)
                            for n, rhs in _split_decl_items(decl.group(2)):
                                if kind in {"input", "output", "inout"}:
                                    mod.ports[n] = PortDef(name=n, direction=kind, line=stmt_line)
                                mod.signals[n] = SignalDef(name=n, line=stmt_line, kind=kind)
                                if rhs:
                                    mod.assignments.append(
                                        Assignment(lhs=[n], rhs=_ids_in_expr(rhs), line=stmt_line, text=f"{n} = {rhs}")
                                    )
                        elif "(" not in one:
                            tdecl = TYPE_DECL_RE.match(one)
                            if tdecl:
                                kind = tdecl.group(1)
                                if kind not in KEYWORDS:
                                    for n, rhs in _split_decl_items(tdecl.group(2)):
                                        mod.signals[n] = SignalDef(name=n, line=stmt_line, kind=kind)
                                        if rhs:
                                            mod.assignments.append(
                                                Assignment(lhs=[n], rhs=_ids_in_expr(rhs), line=stmt_line, text=f"{n} = {rhs}")
                                            )

                        assign = ASSIGN_RE.match(one)
                        if assign:
                            lhs = _ids_in_expr(assign.group(1))
                            rhs = _ids_in_expr(assign.group(2))
                            mod.assignments.append(
                                Assignment(lhs=lhs, rhs=rhs, line=stmt_line, text=one[:-1].strip())
                            )

                        inst = INST_RE.match(one)
                        if inst:
                            module_type, param_raw, inst_name, conn_raw = inst.group(1), inst.group(2) or "", inst.group(3), inst.group(4)
                            connections = _parse_named_assoc(conn_raw)
                            positional = _parse_positional_assoc(conn_raw)
                            parameters = _parse_named_assoc(param_raw)
                            parameter_positional = _parse_positional_assoc(param_raw)
                            mod.instances.append(
                                InstanceDef(
                                    module_type=module_type,
                                    name=inst_name,
                                    connections=connections,
                                    parameters=parameters,
                                    parameter_positional=parameter_positional,
                                    line=stmt_line,
                                    positional=positional,
                                )
                            )
                    stmt = ""

                i += 1

            db.modules[mod_name] = mod
            i += 1

    return db


def read_filelist(filelist_path: str) -> List[str]:
    """Read a VCS-style filelist and return the list of resolved source paths.

    A convenience wrapper around :func:`read_filelist_with_args` that discards
    the compiler-argument list.

    Args:
        filelist_path: Path to the ``.f`` filelist file.

    Returns:
        List of resolved source file paths.
    """
    files, _ = read_filelist_with_args(filelist_path)
    return files


def _resolve_rel(base: str, s: str) -> str:
    if os.path.isabs(s):
        return s

    candidates = []
    cur = base
    while True:
        candidates.append(os.path.normpath(os.path.join(cur, s)))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    candidates.append(os.path.normpath(s))

    path = candidates[0]
    for c in candidates:
        if os.path.exists(c):
            path = c
            break
    return path


def _expand_env(s: str) -> str:
    # Support ${VAR} / $VAR in filelists.
    return os.path.expandvars(s)


def read_filelist_with_args(filelist_path: str, _seen: set[str] | None = None) -> tuple[List[str], List[str]]:
    """Read a VCS-style filelist and return source files and compiler args.

    Supported tokens
    ----------------
    * ``-f`` / ``-F <path>`` — include a nested filelist (circular includes
      are detected and skipped)
    * ``+incdir+<dir>`` — include directory (resolved relative to filelist)
    * ``+define+<M>`` / ``+libext+<ext>`` — passed through verbatim
    * ``-I<dir>`` / ``-I <dir>`` — include path
    * ``-D<macro>`` / ``-y <dir>`` / ``-v <file>`` — passed through
    * bare path — treated as a source file

    Block (``/* … */``) and line (``//``) comments are stripped. Environment
    variables (``${VAR}`` / ``$VAR``) are expanded.

    Args:
        filelist_path: Path to the filelist file.
        _seen: Internal set used to prevent circular inclusion; callers should
            not pass this argument.

    Returns:
        Tuple ``(source_files, extra_args)`` where *source_files* is a list of
        resolved source file paths and *extra_args* is a list of compiler-flag
        strings (suitable for passing to slang or verilator).
    """
    out_files: List[str] = []
    out_args: List[str] = []
    if _seen is None:
        _seen = set()
    abs_fl = os.path.abspath(filelist_path)
    if abs_fl in _seen:
        return out_files, out_args
    _seen.add(abs_fl)

    base = os.path.dirname(os.path.abspath(filelist_path))
    in_block_comment = False
    with open(filelist_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            # Treat only leading comments as comments, so paths like
            # /tmp/a//b/file.sv are preserved.
            if in_block_comment:
                end = line.find("*/")
                if end < 0:
                    continue
                in_block_comment = False
                line = line[end + 2 :]

            while True:
                lead = line.lstrip()
                if not lead:
                    line = ""
                    break
                if lead.startswith("//"):
                    line = ""
                    break
                if lead.startswith("/*"):
                    end = lead.find("*/", 2)
                    if end < 0:
                        in_block_comment = True
                        line = ""
                        break
                    # Keep possible trailing text after a one-line block comment.
                    pad = len(line) - len(lead)
                    line = (" " * pad) + lead[end + 2 :]
                    continue
                break

            s = line.strip()
            if not s or s.startswith("#"):
                continue
            tokens = shlex.split(s, comments=False, posix=True)
            if not tokens:
                continue

            t0 = tokens[0]

            if t0 in {"-f", "-F"} and len(tokens) >= 2:
                nested = _resolve_rel(base, _expand_env(tokens[1]))
                nf, na = read_filelist_with_args(nested, _seen)
                out_files.extend(nf)
                out_args.extend(na)
                continue

            if s.startswith("+incdir+"):
                parts = s.split("+")[2:]
                norm_parts = []
                for p in parts:
                    if not p:
                        continue
                    norm_parts.append(_resolve_rel(base, _expand_env(p)))
                if norm_parts:
                    out_args.append("+incdir+" + "+".join(norm_parts))
                continue

            if s.startswith("+define+") or s.startswith("+libext+"):
                out_args.append(s)
                continue

            if t0 in {"-I", "-y", "-v"} and len(tokens) >= 2:
                out_args.append(t0)
                out_args.append(_resolve_rel(base, _expand_env(tokens[1])))
                continue

            if t0.startswith("-I") and len(t0) > 2:
                out_args.append("-I")
                out_args.append(_resolve_rel(base, _expand_env(t0[2:])))
                continue

            if t0.startswith("-D") or t0 == "-D":
                out_args.extend(tokens)
                continue

            if t0.startswith("-") or t0.startswith("+"):
                out_args.extend(tokens)
                continue

            out_files.append(_resolve_rel(base, _expand_env(s)))
    return out_files, out_args


def discover_sv_files(root: str) -> List[str]:
    """Recursively discover all SV / Verilog source files under *root*.

    Args:
        root: Directory to search.

    Returns:
        Sorted list of file paths with ``.sv``, ``.v``, ``.svh``, or ``.vh``
        extensions.
    """
    files: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith((".sv", ".v", ".svh", ".vh")):
                files.append(os.path.join(dirpath, name))
    files.sort()
    return files
