from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class WaveSignal:
    name: str
    code: str
    changes: List[Tuple[int, str]] = field(default_factory=list)


@dataclass
class WaveDB:
    signals: Dict[str, WaveSignal] = field(default_factory=dict)
    times: List[int] = field(default_factory=list)
    source_file: Optional[str] = None


def _parse_vcd_lines(lines: Iterable[str], source_file: str) -> WaveDB:
    code_to_names: Dict[str, List[str]] = {}
    name_to_last: Dict[str, str] = {}
    current_scope: List[str] = []
    db = WaveDB(source_file=source_file)

    in_header = True
    t = 0

    def touch_time(tt: int) -> None:
        if not db.times:
            db.times.append(tt)
            db.times.append(tt)
        else:
            db.times[1] = tt
    for line in lines:
        s = line.strip()
        if not s:
            continue

        if in_header:
            if s.startswith("$scope"):
                parts = s.split()
                if len(parts) >= 3:
                    current_scope.append(parts[2])
                continue
            if s.startswith("$upscope"):
                if current_scope:
                    current_scope.pop()
                continue
            if s.startswith("$var"):
                parts = s.split()
                if len(parts) >= 5:
                    code = parts[3]
                    base = parts[4]
                    name = ".".join(current_scope + [base]) if current_scope else base
                    code_to_names.setdefault(code, []).append(name)
                    db.signals[name] = WaveSignal(name=name, code=code)
                continue
            if s.startswith("$enddefinitions"):
                in_header = False
                continue
            continue

        if s.startswith("#"):
            t = int(s[1:])
            touch_time(t)
            continue

        if s[0] in "01xXzZuUwWlLhH-":
            v = s[0]
            code = s[1:]
            names = code_to_names.get(code, [])
            for name in names:
                if name_to_last.get(name) != v:
                    db.signals[name].changes.append((t, v))
                    name_to_last[name] = v
            continue

        if s[0] in "br":
            parts = s.split()
            if len(parts) == 2:
                v = parts[0][1:]
                code = parts[1]
                names = code_to_names.get(code, [])
                for name in names:
                    if name_to_last.get(name) != v:
                        db.signals[name].changes.append((t, v))
                        name_to_last[name] = v
            continue

    return db


def _parse_vcd_header(lines: Iterable[str], source_file: str) -> WaveDB:
    current_scope: List[str] = []
    db = WaveDB(source_file=source_file)
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("$scope"):
            parts = s.split()
            if len(parts) >= 3:
                current_scope.append(parts[2])
            continue
        if s.startswith("$upscope"):
            if current_scope:
                current_scope.pop()
            continue
        if s.startswith("$var"):
            parts = s.split()
            if len(parts) >= 5:
                code = parts[3]
                base = parts[4]
                name = ".".join(current_scope + [base]) if current_scope else base
                db.signals[name] = WaveSignal(name=name, code=code)
            continue
        if s.startswith("$enddefinitions"):
            break
    return db


def load_wave(path: str, parse_changes: bool = True) -> WaveDB:
    if path.endswith(".fst"):
        cmd = ["fst2vcd", "-f", path]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as exc:
            raise RuntimeError("fst2vcd is required for .fst support. install gtkwave package") from exc
        if proc.stdout is None:
            raise RuntimeError("fst2vcd launch failed: stdout pipe is unavailable")
        if parse_changes:
            db = _parse_vcd_lines(proc.stdout, source_file=path)
        else:
            db = _parse_vcd_header(proc.stdout, source_file=path)
            try:
                proc.terminate()
            except Exception:
                pass
        stderr = ""
        if proc.stderr is not None:
            stderr = proc.stderr.read()
        rc = proc.wait()
        if parse_changes and rc != 0:
            msg = stderr.strip() or f"fst2vcd failed rc={rc}"
            raise RuntimeError(msg)
        return db

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        if parse_changes:
            return _parse_vcd_lines(f, source_file=path)
        return _parse_vcd_header(f, source_file=path)
