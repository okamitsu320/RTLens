from __future__ import annotations

import subprocess


def _retryable_ortho_failure(stderr: str) -> bool:
    text = stderr.lower()
    return "maze.c" in text or "chksgraph" in text or "cells[1]" in text


def _fallback_polyline(dot_text: str) -> str:
    return dot_text.replace("splines=ortho", "splines=polyline", 1)


def render_dot_to_svg(dot_text: str, dot_cmd: str = "dot", timeout: int = 8) -> str:
    proc = subprocess.run(
        [dot_cmd, "-Tsvg"],
        input=dot_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        if "splines=ortho" in dot_text and _retryable_ortho_failure(stderr):
            retry = subprocess.run(
                [dot_cmd, "-Tsvg"],
                input=_fallback_polyline(dot_text),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if retry.returncode == 0:
                return retry.stdout
        raise RuntimeError(f"graphviz dot failed: {stderr}")
    return proc.stdout


def render_dot_to_cmapx(dot_text: str, dot_cmd: str = "dot", timeout: int = 8) -> str:
    proc = subprocess.run(
        [dot_cmd, "-Tcmapx"],
        input=dot_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        if "splines=ortho" in dot_text and _retryable_ortho_failure(stderr):
            retry = subprocess.run(
                [dot_cmd, "-Tcmapx"],
                input=_fallback_polyline(dot_text),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if retry.returncode == 0:
                return retry.stdout
        raise RuntimeError(f"graphviz dot failed: {stderr}")
    return proc.stdout


def render_dot_to_png(dot_text: str, dot_cmd: str = "dot", timeout: int = 8) -> bytes:
    proc = subprocess.run(
        [dot_cmd, "-Tpng"],
        input=dot_text.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="ignore").strip() or "(no stderr)"
        if "splines=ortho" in dot_text and _retryable_ortho_failure(stderr):
            retry = subprocess.run(
                [dot_cmd, "-Tpng"],
                input=_fallback_polyline(dot_text).encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if retry.returncode == 0:
                return retry.stdout
        raise RuntimeError(f"graphviz dot failed: {stderr}")
    return proc.stdout
