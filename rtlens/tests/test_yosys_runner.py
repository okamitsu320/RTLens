from __future__ import annotations

from pathlib import Path

import rtlens.yosys_runner as yosys_runner


def test_node_script_path_detection() -> None:
    assert yosys_runner._is_node_script_path("netlistsvg.js")
    assert yosys_runner._is_node_script_path("netlistsvg.mjs")
    assert not yosys_runner._is_node_script_path("netlistsvg.cmd")


def test_windows_cmd_wrapper_detection() -> None:
    assert yosys_runner._is_windows_cmd_wrapper("netlistsvg.cmd")
    assert yosys_runner._is_windows_cmd_wrapper("netlistsvg.bat")
    assert not yosys_runner._is_windows_cmd_wrapper("netlistsvg.js")


def test_netlistsvg_candidates_skip_node_wrapper_for_cmd_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(yosys_runner.os, "name", "nt", raising=False)
    monkeypatch.setattr(yosys_runner.shutil, "which", lambda _name: r"C:\Users\me\AppData\Roaming\npm\netlistsvg.CMD")
    monkeypatch.setattr(yosys_runner.os.path, "isfile", lambda _p: True)

    candidates = yosys_runner._netlistsvg_command_candidates("", "netlistsvg")
    assert all(not (cmd and cmd[0] == "node") for cmd in candidates)
    assert [r"C:\Users\me\AppData\Roaming\npm\netlistsvg.CMD"] in candidates


def test_netlistsvg_candidates_keep_node_wrapper_for_js(monkeypatch) -> None:
    monkeypatch.setattr(yosys_runner.os, "name", "posix", raising=False)
    monkeypatch.setattr(yosys_runner.shutil, "which", lambda _name: "/tmp/netlistsvg.js")
    monkeypatch.setattr(yosys_runner.os.path, "isfile", lambda _p: True)

    candidates = yosys_runner._netlistsvg_command_candidates("", "netlistsvg")
    assert ["node", "--stack_size=65500", "/tmp/netlistsvg.js"] in candidates


def test_netlistsvg_candidates_from_patched_dir(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "netlistsvg.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("console.log('ok')\n", encoding="utf-8")

    candidates = yosys_runner._netlistsvg_command_candidates(str(tmp_path), "netlistsvg")
    assert candidates[0] == ["node", "--stack_size=65500", str(script)]
