from __future__ import annotations

import os
import shlex
import string
from typing import List


_ALLOWED_FIELDS = {"file", "fileq", "line", "basename", "dir"}


def build_editor_argv(template: str, file_path: str, line: int) -> List[str]:
    tpl = str(template or "").strip()
    if not tpl:
        raise ValueError("empty template")

    formatter = string.Formatter()
    for _literal, field_name, _format_spec, _conversion in formatter.parse(tpl):
        if field_name is None:
            continue
        if field_name not in _ALLOWED_FIELDS:
            raise ValueError(f"unsupported placeholder: {field_name}")

    mapping = {
        # {file} and {fileq} are intentionally equivalent for compatibility.
        # Both keep space-containing paths as a single argv token.
        "file": shlex.quote(str(file_path)),
        "fileq": shlex.quote(str(file_path)),
        "line": int(line),
        "basename": shlex.quote(os.path.basename(str(file_path))),
        "dir": shlex.quote(os.path.dirname(os.path.abspath(str(file_path)))),
    }
    try:
        expanded = tpl.format(**mapping)
    except Exception as exc:
        raise ValueError(f"invalid template: {exc}") from exc

    try:
        argv = shlex.split(expanded)
    except ValueError as exc:
        raise ValueError(f"invalid command tokens: {exc}") from exc
    if not argv:
        raise ValueError("empty command after expansion")
    return argv
