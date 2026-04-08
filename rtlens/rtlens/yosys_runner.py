"""Subprocess execution utilities for yosys / netlistsvg pipeline stages.

All functions here are pure command-execution helpers with no Qt or SVG
dependencies.  They can be imported from worker threads or CLI scripts
without pulling in the rest of the netlistsvg pipeline.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set


def _is_node_script_path(path: str) -> bool:
    """Return True when *path* points to a JavaScript file suitable for node."""
    suffix = Path(str(path or "")).suffix.lower()
    return suffix in {".js", ".mjs", ".cjs"}


def _is_windows_cmd_wrapper(path: str) -> bool:
    """Return True when *path* is a Windows command-wrapper script."""
    suffix = Path(str(path or "")).suffix.lower()
    return suffix in {".cmd", ".bat", ".ps1"}


def _netlistsvg_command(netlistsvg_dir: str, netlistsvg_cmd: str) -> List[str]:
    """Return the base command list for invoking netlistsvg."""
    if netlistsvg_dir:
        script = Path(netlistsvg_dir) / "bin" / "netlistsvg.js"
        if script.is_file():
            return ["node", str(script)]
    return [netlistsvg_cmd]


def _netlistsvg_command_candidates(netlistsvg_dir: str, netlistsvg_cmd: str) -> List[List[str]]:
    """Return an ordered list of candidate command lists for netlistsvg, deduped."""
    primary = _netlistsvg_command(netlistsvg_dir, netlistsvg_cmd)
    candidates: List[List[str]] = []
    if len(primary) >= 2 and primary[0] == "node" and primary[1].endswith("netlistsvg.js"):
        candidates.append(["node", "--stack_size=65500", primary[1]])
        candidates.append(primary)
        if netlistsvg_cmd and netlistsvg_cmd != "node":
            candidates.append([netlistsvg_cmd])
    else:
        if len(primary) == 1:
            resolved = shutil.which(primary[0])
            if resolved and os.path.isfile(resolved):
                if _is_node_script_path(resolved):
                    candidates.append(["node", "--stack_size=65500", resolved])
                elif os.name == "nt" and _is_windows_cmd_wrapper(resolved):
                    candidates.append([resolved])
        candidates.append(primary)
    dedup: List[List[str]] = []
    seen: Set[tuple[str, ...]] = set()
    for cmd in candidates:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(cmd)
    return dedup


def _coerce_text_blob(value) -> str:
    """Coerce bytes or None to a plain str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _emit_progress_event(
    progress_cb: Optional[Callable[[dict], None]],
    event: dict,
) -> None:
    """Call progress_cb(event) if provided, swallowing any exception."""
    if progress_cb is None:
        return
    try:
        progress_cb(event)
    except Exception:
        return


def _emit_metric_event(
    progress_cb: Optional[Callable[[dict], None]],
    stage: str,
    module: str,
    elapsed_sec: float,
    **extra,
) -> None:
    """Emit a structured 'metric' progress event for a named pipeline stage."""
    payload = {
        "event": "metric",
        "stage": str(stage or ""),
        "module": str(module or ""),
        "elapsed_sec": float(max(0.0, elapsed_sec)),
    }
    for k, v in extra.items():
        payload[str(k)] = v
    _emit_progress_event(progress_cb, payload)


def _run_command_with_heartbeat(
    cmd: List[str],
    timeout_sec: int,
    progress_cb: Optional[Callable[[dict], None]] = None,
    heartbeat_sec: int = 5,
    progress_meta: Optional[dict] = None,
) -> tuple[Optional[subprocess.CompletedProcess], Optional[subprocess.TimeoutExpired]]:
    """Run *cmd* in a daemon thread, emitting heartbeat events while waiting.

    Returns ``(CompletedProcess, None)`` on success or ``(None, TimeoutExpired)``
    on timeout.  Raises ``RuntimeError`` for any other subprocess failure.
    """
    hb = max(1, int(heartbeat_sec))
    meta = dict(progress_meta or {})
    started = time.perf_counter()
    result_holder: dict[str, object] = {}

    def _runner() -> None:
        try:
            result_holder["proc"] = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=max(1, int(timeout_sec)),
            )
        except subprocess.TimeoutExpired as e:
            result_holder["timeout"] = e
        except Exception as e:
            result_holder["error"] = e

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    _emit_progress_event(
        progress_cb,
        {
            "event": "start",
            "cmd": list(cmd),
            "timeout_sec": int(timeout_sec),
            "elapsed_sec": 0.0,
            **meta,
        },
    )
    deadline = started + max(1, int(timeout_sec))
    next_tick = started + hb
    while th.is_alive():
        th.join(timeout=0.2)
        now = time.perf_counter()
        if now >= next_tick:
            elapsed = max(0.0, now - started)
            remaining = max(0.0, deadline - now)
            _emit_progress_event(
                progress_cb,
                {
                    "event": "heartbeat",
                    "cmd": list(cmd),
                    "timeout_sec": int(timeout_sec),
                    "elapsed_sec": elapsed,
                    "remaining_sec": remaining,
                    **meta,
                },
            )
            next_tick += hb
            if next_tick <= now:
                next_tick = now + hb
    now = time.perf_counter()
    err = result_holder.get("error")
    if err is not None:
        raise RuntimeError(str(err))
    timeout_exc = result_holder.get("timeout")
    if isinstance(timeout_exc, subprocess.TimeoutExpired):
        elapsed = max(0.0, now - started)
        _emit_progress_event(
            progress_cb,
            {
                "event": "timeout",
                "cmd": list(cmd),
                "timeout_sec": int(timeout_sec),
                "elapsed_sec": elapsed,
                "remaining_sec": 0.0,
                **meta,
            },
        )
        return None, timeout_exc
    proc = result_holder.get("proc")
    if not isinstance(proc, subprocess.CompletedProcess):
        raise RuntimeError("subprocess worker returned no result")
    elapsed = max(0.0, now - started)
    _emit_progress_event(
        progress_cb,
        {
            "event": "end",
            "cmd": list(cmd),
            "timeout_sec": int(timeout_sec),
            "elapsed_sec": elapsed,
            "returncode": int(proc.returncode),
            **meta,
        },
    )
    return proc, None


def _run_netlistsvg_from_json(
    json_path: Path,
    svg_path: Path,
    netlistsvg_cmd: str,
    netlistsvg_dir: str,
    timeout_sec: int,
    progress_cb: Optional[Callable[[dict], None]] = None,
    heartbeat_sec: int = 5,
    progress_meta: Optional[dict] = None,
) -> tuple[bool, str, str]:
    """Invoke netlistsvg on *json_path*, writing SVG to *svg_path*.

    Tries each candidate command in turn.  Returns ``(ok, combined_log, error_msg)``.
    """
    svg_proc = None
    candidate_logs: List[str] = []
    for cmd in _netlistsvg_command_candidates(netlistsvg_dir, netlistsvg_cmd):
        run_cmd = cmd + [str(json_path), "-o", str(svg_path)]
        cur = None
        try:
            cur, timeout_exc = _run_command_with_heartbeat(
                run_cmd,
                timeout_sec=timeout_sec,
                progress_cb=progress_cb,
                heartbeat_sec=heartbeat_sec,
                progress_meta=progress_meta,
            )
        except Exception as e:
            candidate_logs.append(
                "netlistsvg command: "
                + " ".join(shlex.quote(x) for x in run_cmd)
                + "\nnetlistsvg launch failed:\n"
                + str(e)
            )
            continue
        if timeout_exc is not None:
            e = timeout_exc
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
            + (cur.stdout.strip() or "(none)")
            + "\nnetlistsvg stderr:\n"
            + (cur.stderr.strip() or "(none)")
        )
        if cur.returncode == 0 and svg_path.is_file():
            svg_proc = cur
            break
        svg_proc = cur
    logs = "\n\n".join(candidate_logs)
    if svg_proc is None or svg_proc.returncode != 0 or not svg_path.is_file():
        err = (svg_proc.stderr.strip() if svg_proc and svg_proc.stderr else "") or "netlistsvg failed"
        return False, logs, err
    return True, logs, ""
