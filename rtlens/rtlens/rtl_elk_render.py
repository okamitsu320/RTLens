from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _default_elk_runner() -> Path:
    return Path(__file__).resolve().parents[2] / "third_party" / "elk" / "run_elk.js"


def render_elk_layout(graph: Dict[str, Any], node_cmd: str = "node", timeout: int | None = 8) -> Dict[str, Any]:
    runner = _default_elk_runner()
    if not runner.is_file():
        raise RuntimeError(f"ELK runner not found: {runner}")
    try:
        proc = subprocess.run(
            [node_cmd, str(runner)],
            input=json.dumps(graph),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"elk layout failed: node command not found: {node_cmd}. "
            "Install Node.js and ensure it is available on PATH."
        ) from e
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        if "Cannot find module './node_modules/elkjs/lib/elk.bundled.js'" in stderr or "Cannot find module 'elkjs'" in stderr:
            hint = f"hint: install ELK dependency: (cd {runner.parent} && npm ci)"
            raise RuntimeError(f"elk layout failed: {stderr}\n{hint}")
        raise RuntimeError(f"elk layout failed: {stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"elk output parse failed: {e}") from e


def benchmark_elk_layouts(
    variants: List[tuple[str, Dict[str, Any]]],
    node_cmd: str = "node",
    timeout: int | None = 8,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for name, graph in variants:
        children = len(graph.get("children", []))
        edges = len(graph.get("edges", []))
        t0 = time.perf_counter()
        try:
            layout = render_elk_layout(graph, node_cmd=node_cmd, timeout=timeout)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            size = layout.get("width", 0), layout.get("height", 0)
            results.append(
                {
                    "name": name,
                    "status": "ok",
                    "elapsed_ms": elapsed_ms,
                    "children": children,
                    "edges": edges,
                    "width": round(float(size[0]), 1) if size[0] else 0,
                    "height": round(float(size[1]), 1) if size[1] else 0,
                }
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            results.append(
                {
                    "name": name,
                    "status": "timeout",
                    "elapsed_ms": elapsed_ms,
                    "children": children,
                    "edges": edges,
                }
            )
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            results.append(
                {
                    "name": name,
                    "status": "error",
                    "elapsed_ms": elapsed_ms,
                    "children": children,
                    "edges": edges,
                    "error": str(e),
                }
            )
    return results
