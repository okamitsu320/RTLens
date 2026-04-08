from __future__ import annotations

import os
import re
from typing import List, Optional

_RTLENS_TMP_MARKERS = ("rtlens_schematic_prebuild_", "rtlens_netlistsvg_")


def _is_rtlens_generated_tmp_src(path_text: str) -> bool:
    """Return True when a source path points to rtlens-generated temp files."""
    text = str(path_text or "")
    if not text:
        return False
    return any(marker in text for marker in _RTLENS_TMP_MARKERS)


def normalize_schematic_src(raw_src: str, fallback: str = "") -> str:
    """Normalize a `file:line` source attribute used by schematic overlays."""
    src = str(raw_src or "").strip()
    fb = str(fallback or "").strip()
    if not src:
        return fb
    m = re.match(r"^(.*?):(\d+)", src)
    if not m:
        return fb or src
    file_part = m.group(1)
    line_part = m.group(2)
    if _is_rtlens_generated_tmp_src(file_part):
        return fb or ""
    if os.path.isfile(file_part):
        return f"{file_part}:{line_part}"
    return fb or src


def canonical_schematic_name(name: str) -> str:
    """Return a normalized schematic symbol/module name."""
    out = str(name or "").strip()
    while out.startswith("\\"):
        out = out[1:]
    return out


def demangle_paramod_module_name(cell_type: str) -> str:
    """Extract base module name from Yosys `$paramod...` names.

    Yosys emits multiple encodings for parameterized modules, for example:
    - ``$paramod$my_mod$WIDTH=s32'...``
    - ``$paramod\\my_mod\\WIDTH=s32'...``
    """
    raw = str(cell_type or "").strip()
    if not raw:
        return ""
    if not raw.startswith("$paramod"):
        return ""

    rest = raw[len("$paramod") :]
    rest = rest.lstrip("$\\")
    rest = canonical_schematic_name(rest)
    if not rest:
        return ""

    # First, try to split on parameter assignment marker boundaries.
    for sep in ("\\", "$"):
        if sep not in rest:
            continue
        head, tail = rest.split(sep, 1)
        if head and "=" in tail:
            return canonical_schematic_name(head)

    # Fallback: split at first separator when no explicit assignment marker.
    if "$" in rest:
        return canonical_schematic_name(rest.split("$", 1)[0])
    if "\\" in rest:
        return canonical_schematic_name(rest.split("\\", 1)[0])
    return canonical_schematic_name(rest)


def classify_schematic_cell_type(cell_type: str) -> tuple[str, str]:
    """Classify netlist cell references into user instance vs primitive cell."""
    ctype = canonical_schematic_name(cell_type)
    if not ctype:
        return "cell", ""
    if ctype.startswith("$"):
        base = demangle_paramod_module_name(ctype)
        if base:
            return "instance", base
        return "cell", ctype
    return "instance", ctype


def cleanup_wave_name(name: str) -> str:
    """Normalize clipboard/imported wave names to design query form."""
    text = name.strip()
    if not text:
        return text
    text = text.strip("'\"")
    text = text.replace("/", ".")
    while ".." in text:
        text = text.replace("..", ".")
    while text.startswith(".") or text.startswith("/"):
        text = text[1:]
    text = re.sub(r"\[[^\]]+\]", "", text)
    return text


def extract_wave_name_candidates(text: str) -> List[str]:
    """Extract unique candidate signal names from clipboard text."""
    t = text.strip()
    if not t:
        return []
    out: List[str] = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        token = s.split()[0]
        if "." in token or "/" in token or re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", token):
            out.append(token)
    for m in re.finditer(r"[A-Za-z0-9_$./\\\[\]:]+", t):
        token = m.group(0)
        if "." in token or "/" in token or re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", token):
            out.append(token)
    uniq: List[str] = []
    seen = set()
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


def parse_jump_item(text: str) -> Optional[tuple[str, int, str, str]]:
    """Parse list items in the `<signal> -> <file>:<line>` display format."""
    if " -> " not in text:
        return None
    sig, loc = text.split(" -> ", 1)
    if ":" not in loc:
        return None
    file_part, line_s = loc.rsplit(":", 1)
    try:
        line = int(line_s)
    except ValueError:
        return None
    token = sig.split(".")[-1]
    return file_part, line, token, sig
