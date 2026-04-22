from __future__ import annotations

from pathlib import Path

import rtlens.editor_config as editor_config


def test_editor_config_path_uses_xdg_config_home(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    path = editor_config.editor_config_path()

    assert path == xdg / "rtlens" / "editor_qt.json"


def test_load_editor_template_returns_empty_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert editor_config.load_editor_template() == ""


def test_save_then_load_editor_template_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    editor_config.save_editor_template("code --goto {file}:{line}")

    assert editor_config.load_editor_template() == "code --goto {file}:{line}"


def test_load_editor_template_returns_empty_on_invalid_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = editor_config.editor_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ invalid json", encoding="utf-8")

    assert editor_config.load_editor_template() == ""


def test_detect_available_presets_uses_which_and_excludes_custom(monkeypatch) -> None:
    monkeypatch.setattr(editor_config.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(
        editor_config.shutil,
        "which",
        lambda cmd: (f"/usr/bin/{cmd}" if cmd in {"code", "gedit"} else None),
    )

    out = editor_config.detect_available_presets()

    assert out == ["vscode", "vscode_reuse", "gedit"]
    assert "custom" not in out


def test_detect_available_presets_treats_windows_notepad_as_available(monkeypatch) -> None:
    monkeypatch.setattr(editor_config.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(editor_config.shutil, "which", lambda _cmd: None)

    out = editor_config.detect_available_presets()

    assert out == ["notepad"]
