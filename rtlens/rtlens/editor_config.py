from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from collections import OrderedDict
from pathlib import Path
from typing import List


EDITOR_PRESETS = OrderedDict(
    [
        ("vscode", {"label": "VS Code", "template": "code --goto {file}:{line}"}),
        (
            "vscode_reuse",
            {"label": "VS Code (reuse window)", "template": "code --reuse-window --goto {file}:{line}"},
        ),
        ("vim", {"label": "Vim (terminal)", "template": "x-terminal-emulator -e vim +{line} {fileq}"}),
        ("emacs", {"label": "Emacs", "template": "emacs +{line} {fileq}"}),
        ("kate", {"label": "Kate", "template": "kate -l {line} {file}"}),
        ("gedit", {"label": "gedit", "template": "gedit +{line} {file}"}),
        ("sublime", {"label": "Sublime Text", "template": "subl {file}:{line}"}),
        ("notepad", {"label": "Notepad (Windows)", "template": "notepad {fileq}"}),
        ("custom", {"label": "Custom", "template": ""}),
    ]
)


def editor_config_path() -> Path:
    """Return ~/.config/rtlens/editor_qt.json with XDG_CONFIG_HOME support."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))
    return cfg / "rtlens" / "editor_qt.json"


def load_editor_template() -> str:
    """Load saved editor template. Returns empty string on missing/invalid config."""
    p = editor_config_path()
    if not p.is_file():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("editor_cmd")
    return value if isinstance(value, str) else ""


def save_editor_template(template: str) -> None:
    """Persist editor template as JSON { "editor_cmd": "<template>" }."""
    p = editor_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"editor_cmd": str(template)}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _template_command_name(template: str) -> str:
    raw = str(template or "").strip()
    if not raw:
        return ""
    try:
        argv = shlex.split(raw)
    except ValueError:
        return ""
    return str(argv[0]).strip() if argv else ""


def detect_available_presets() -> List[str]:
    """Return preset keys whose command appears available in PATH."""
    out: List[str] = []
    for key, meta in EDITOR_PRESETS.items():
        if key == "custom":
            continue
        if key == "notepad" and sys.platform == "win32":
            out.append(key)
            continue
        cmd = _template_command_name(str(meta.get("template", "")))
        if cmd and shutil.which(cmd):
            out.append(key)
    return out
