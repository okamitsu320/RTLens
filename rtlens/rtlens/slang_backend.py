"""slang-based SystemVerilog elaboration backend.

This module wraps the ``slang_dump`` C++ binary (built on demand from
``rtlens/tools/slang_dump.cpp``) to perform full IEEE-1800 elaboration of a
SystemVerilog design.  The binary emits a tab-separated stream of tagged
records; this module parses that stream into a :class:`~rtlens.model.DesignDB`
and :class:`~rtlens.model.ConnectivityDB`.

Record tags emitted by ``slang_dump``
--------------------------------------
``H``  — hierarchy node (instance path, module name, source location)
``S``  — signal declaration
``D``  — driver site (non-port assignment)
``DP`` — driver site via port
``LD`` — load site (data)
``LP`` — load site (port)
``LC`` — load site (control)
``E``  — data-flow edge (src → dst)
``ED`` — data-flow edge (data)
``EC`` — data-flow edge (control)
``EP`` — port-alias edge
``MD`` — module / interface / program definition
``SD`` — subroutine (function / task) definition
``MR`` — module reference (instantiation site)
``SR`` — subroutine call site

On first use the binary is compiled from source using a standalone slang
install-prefix. Subsequent calls skip recompilation if the binary is newer
than the source.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .model import ConnectivityDB, DesignDB, HierNode, SourceLoc


class SlangBackendError(RuntimeError):
    pass


# Tag embedded in toolchain meta to track the current CMakeLists build recipe.
# Bump this string whenever the generated CMakeLists.txt template changes in a
# way that requires the cached slang_dump binary to be rebuilt.
_CMAKELISTS_RECIPE_TAG = "static-mingw-runtime-v4-prefix-toolchain"
_SLANG_PREFIX_TOOLCHAIN_META_REL = Path("share/rtlens/slang_toolchain_meta.json")
_WINDOWS_NATIVE_CRASH_CACHE: Dict[str, str] = {}


def _repo_root() -> Path:
    """Return the repository root (two directories above this file's package)."""
    return Path(__file__).resolve().parents[2]


def _tool_src() -> Path:
    """Return the path to the ``slang_dump.cpp`` source file."""
    return _repo_root() / "rtlens" / "tools" / "slang_dump.cpp"


def _tool_bin() -> Path:
    """Return the path to the compiled ``slang_dump`` binary."""
    return _repo_root() / "rtlens" / "bin" / "slang_dump"


def _tool_meta() -> Path:
    """Return the path to toolchain metadata for ``slang_dump``."""
    return _repo_root() / "rtlens" / "bin" / "slang_dump.meta"


def _read_tool_meta() -> str:
    """Read toolchain metadata for ``slang_dump`` if available."""
    meta = _tool_meta()
    if not meta.exists():
        return "cached (toolchain metadata unavailable)"
    try:
        text = meta.read_text(encoding="utf-8").strip()
        if not text:
            return "cached (toolchain metadata unavailable)"
        joined = " | ".join(line.strip() for line in text.splitlines() if line.strip())
        if len(joined) > 220:
            return joined[:217] + "..."
        return joined
    except Exception:
        return "cached (toolchain metadata unreadable)"


def _write_tool_meta(toolchain_info: str) -> None:
    """Write toolchain metadata for ``slang_dump``."""
    meta = _tool_meta()
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(toolchain_info.strip() + "\n", encoding="utf-8")


def _resolve_executable_path(path_or_name: str, *, search_path: str = "") -> str:
    """Resolve an executable from a command name/path and return canonical text."""
    value = str(path_or_name or "").strip()
    if not value:
        return ""
    found = shutil.which(value, path=search_path or None)
    if found:
        value = found
    p = Path(value).expanduser()
    if p.exists():
        try:
            return str(p.resolve())
        except Exception:
            return str(p)
    return value


def _read_slang_prefix_toolchain_meta(prefix: Optional[Path]) -> Dict[str, object]:
    """Read standalone slang-prefix toolchain metadata when available."""
    if prefix is None:
        return {}
    meta = prefix / _SLANG_PREFIX_TOOLCHAIN_META_REL
    if not meta.exists():
        return {}
    try:
        parsed = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, object] = {}
    for key, value in parsed.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
    out["meta_path"] = str(meta)
    return out


def _meta_text(meta: Dict[str, object], key: str) -> str:
    return str(meta.get(key, "") or "").strip()


def _unique_paths(candidates: Sequence[Path]) -> List[Path]:
    """Return path candidates with stable de-duplication."""
    seen: Set[str] = set()
    uniq: List[Path] = []
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(cand)
    return uniq


def _normalize_path_key(value: str) -> str:
    """Return a normalized key for path de-duplication."""
    return os.path.normcase(os.path.normpath(value))


def _resolve_existing_dir(path_value: str) -> Optional[Path]:
    """Resolve a directory path from an executable or directory string."""
    if not path_value:
        return None
    p = Path(path_value).expanduser()
    if p.is_dir():
        return p
    if p.exists():
        return p.parent
    return None


def _prepend_env_path(env: Dict[str, str], candidates: Sequence[Path]) -> List[str]:
    """Prepend existing candidate directories to PATH and return inserted entries."""
    existing = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    prepended: List[str] = []
    prepended_keys: Set[str] = set()
    for cand in candidates:
        try:
            resolved = str(cand.resolve())
        except Exception:
            resolved = str(cand)
        if not resolved:
            continue
        if not Path(resolved).is_dir():
            continue
        key = _normalize_path_key(resolved)
        if key in prepended_keys:
            continue
        prepended.append(resolved)
        prepended_keys.add(key)

    merged_existing: List[str] = []
    for part in existing:
        if _normalize_path_key(part) in prepended_keys:
            continue
        merged_existing.append(part)
    env["PATH"] = os.pathsep.join(prepended + merged_existing)
    return prepended


def _collect_windows_runtime_snapshot(
    *,
    tool: Path,
    run_env: Dict[str, str],
    runtime_path_prepend: Sequence[str],
) -> Dict[str, object]:
    """Collect runtime toolchain details for Windows crash diagnostics."""
    env_path = str(run_env.get("PATH", "") or "")

    def _which(name: str) -> str:
        return _resolve_executable_path(name, search_path=env_path)

    path_parts = [p for p in env_path.split(os.pathsep) if p]
    return {
        "tool": str(tool),
        "runtime_path_prepend": list(runtime_path_prepend),
        "path_head": path_parts[:20],
        "resolved": {
            "cmake": _which("cmake"),
            "g++": _which("g++"),
            "gcc": _which("gcc"),
            "mingw32-make": _which("mingw32-make"),
            "ninja": _which("ninja"),
            "nmake": _which("nmake"),
        },
        "env_overrides": {
            "RTLENS_CMAKE_GENERATOR": str(os.environ.get("RTLENS_CMAKE_GENERATOR", "") or ""),
            "RTLENS_C_COMPILER": str(os.environ.get("RTLENS_C_COMPILER", "") or ""),
            "RTLENS_CXX_COMPILER": str(os.environ.get("RTLENS_CXX_COMPILER", "") or ""),
            "RTLENS_CMAKE_MAKE_PROGRAM": str(os.environ.get("RTLENS_CMAKE_MAKE_PROGRAM", "") or ""),
        },
    }


def _windows_toolchain_mismatch_lines(
    prefix_meta: Dict[str, object],
    runtime_snapshot: Dict[str, object],
) -> List[str]:
    """Compare slang-prefix build metadata with active Windows runtime toolchain."""
    if not prefix_meta or not runtime_snapshot:
        return []

    def _meta_text(key: str) -> str:
        value = prefix_meta.get(key, "")
        return str(value or "").strip()

    resolved = runtime_snapshot.get("resolved", {})
    if not isinstance(resolved, dict):
        resolved = {}

    runtime_cxx = _resolve_executable_path(
        str(
            os.environ.get("RTLENS_CXX_COMPILER", "")
            or resolved.get("g++", "")
            or resolved.get("gcc", "")
            or ""
        )
    )
    runtime_make = _resolve_executable_path(
        str(
            os.environ.get("RTLENS_CMAKE_MAKE_PROGRAM", "")
            or resolved.get("mingw32-make", "")
            or resolved.get("ninja", "")
            or resolved.get("nmake", "")
            or ""
        )
    )
    runtime_generator = str(os.environ.get("RTLENS_CMAKE_GENERATOR", "") or "").strip()

    meta_cxx = _resolve_executable_path(_meta_text("cxx_compiler"))
    meta_make = _resolve_executable_path(_meta_text("make_program"))
    meta_generator = _meta_text("cmake_generator")

    lines: List[str] = []
    if meta_cxx and runtime_cxx and _normalize_path_key(meta_cxx) != _normalize_path_key(runtime_cxx):
        lines.append(
            "[rtlens] windows toolchain warning: slang prefix was built with "
            f"cxx={meta_cxx}, but runtime resolves cxx={runtime_cxx}"
        )
    if meta_make and runtime_make and _normalize_path_key(meta_make) != _normalize_path_key(runtime_make):
        lines.append(
            "[rtlens] windows toolchain warning: slang prefix was built with "
            f"make={meta_make}, but runtime resolves make={runtime_make}"
        )
    if meta_generator and runtime_generator and meta_generator != runtime_generator:
        lines.append(
            "[rtlens] windows toolchain warning: slang prefix metadata generator="
            f"{meta_generator}, env override requests {runtime_generator}"
        )
    if lines:
        meta_path = str(prefix_meta.get("meta_path", "") or "").strip()
        if meta_path:
            lines.append(f"[rtlens] windows toolchain metadata source: {meta_path}")
    return lines


def _windows_runtime_path_candidates(tool: Path, root: Path) -> List[Path]:
    """Collect Windows runtime directories required by slang_dump."""
    candidates: List[Path] = [tool.parent]
    try:
        slang_prefix = _resolve_standalone_slang_prefix(root)
    except SlangBackendError:
        slang_prefix = None
    if slang_prefix:
        candidates.extend(
            [
                slang_prefix / "bin",
                slang_prefix / "lib",
                slang_prefix / "lib64",
            ]
        )

    for env_key in ("RTLENS_CXX_COMPILER", "RTLENS_C_COMPILER"):
        value = str(os.environ.get(env_key, "") or "").strip()
        resolved = _resolve_existing_dir(value)
        if resolved:
            candidates.append(resolved)

    for exe in ("g++", "gcc"):
        exe_path = shutil.which(exe)
        if not exe_path:
            continue
        resolved = _resolve_existing_dir(exe_path)
        if resolved:
            candidates.append(resolved)

    for env_key in ("MSYSTEM_PREFIX", "MINGW_PREFIX"):
        value = str(os.environ.get(env_key, "") or "").strip()
        resolved = _resolve_existing_dir(value)
        if not resolved:
            continue
        if resolved.name.lower() == "bin":
            candidates.append(resolved)
        else:
            candidates.append(resolved / "bin")

    return _unique_paths(candidates)


def _is_windows_access_violation(code: int) -> bool:
    """Return True when *code* is Windows native crash-style exit status."""
    return code in (
        3221225477,   # 0xC0000005 STATUS_ACCESS_VIOLATION
        -1073741819,
        3221225620,   # 0xC0000094 STATUS_INTEGER_DIVIDE_BY_ZERO
        -1073741676,
    )


def _normalize_windows_path_text(text: str) -> str:
    """Normalize path separators for Windows CLI args."""
    value = str(text or "")
    if os.name == "nt":
        return value.replace("\\", "/")
    return value


def _normalize_windows_slang_args(args: Sequence[str]) -> List[str]:
    """Normalize include-path style args for Windows slang CLI invocation."""
    out: List[str] = []
    items = [str(x or "") for x in args]
    i = 0
    while i < len(items):
        tok = items[i]
        if tok.startswith("+incdir+"):
            parts = [p for p in tok.split("+")[2:] if p]
            norm = [_normalize_windows_path_text(p) for p in parts]
            out.append("+incdir+" + "+".join(norm))
        elif tok == "-I" and i + 1 < len(items):
            out.extend(["-I", _normalize_windows_path_text(items[i + 1])])
            i += 1
        elif tok.startswith("-I") and len(tok) > 2:
            out.append("-I" + _normalize_windows_path_text(tok[2:]))
        else:
            out.append(tok)
        i += 1
    return out


def _drop_include_args(args: Sequence[str]) -> List[str]:
    """Drop include-directory style args for a Windows crash retry path."""
    out: List[str] = []
    items = [str(x or "") for x in args]
    i = 0
    while i < len(items):
        tok = items[i]
        if tok.startswith("+incdir+"):
            i += 1
            continue
        if tok == "-I":
            i += 2
            continue
        if tok.startswith("-I") and len(tok) > 2:
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


_WINDOWS_SVVIEW_STAGES: List[str] = [
    "hier-scan",
    "hier-visit",
    "hier-defs",
    "signals",
    "ports",
    "assign",
    "use",
    "callable",
    "full",
]


def _build_slang_dump_args(
    tool: Path,
    top: str,
    extra_args: Sequence[str],
    abs_files: Sequence[str],
    stage: str = "",
) -> List[str]:
    """Build command-line arguments for slang_dump."""
    args = [str(tool)]
    if top:
        args.extend(["--rtlens-top", top])
    stage_name = str(stage or "").strip()
    if stage_name:
        args.extend(["--rtlens-stage", stage_name])
    if extra_args:
        args.extend(list(extra_args))
    args.extend(list(abs_files))
    return args


def _run_slang_dump(args: Sequence[str], run_env: Dict[str, str]) -> subprocess.CompletedProcess:
    """Execute slang_dump with the provided command-line arguments."""
    return subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
    )


def _windows_diag_cache_path(root: Path) -> Path:
    """Return the diagnostic cache path for Windows slang stage probing."""
    return root / ".cache" / "slang_dump_windows_diag.json"


def _write_windows_stage_diagnostics(root: Path, payload: dict) -> Optional[Path]:
    """Write Windows stage diagnostics JSON and return the path on success."""
    out = _windows_diag_cache_path(root)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return out
    except Exception:
        return None


def _windows_crash_key(tool: Path, top: str, abs_files: Sequence[str]) -> str:
    """Build a stable cache key for per-session Windows native crash suppression."""
    h = hashlib.sha1()
    h.update(str(tool).encode("utf-8", errors="ignore"))
    h.update(b"\n")
    h.update(str(top or "").encode("utf-8", errors="ignore"))
    for f in abs_files:
        h.update(b"\n")
        h.update(str(f).encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _windows_crash_guard_enabled() -> bool:
    """Return True when session crash suppression should be active."""
    value = str(os.environ.get("RTLENS_WINDOWS_SLANG_RETRY", "") or "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def _minimize_windows_crash_inputs(
    *,
    tool: Path,
    top: str,
    normalized_extra: Sequence[str],
    abs_files: Sequence[str],
    run_env: Dict[str, str],
    stage: str,
) -> Tuple[List[str], List[dict]]:
    """Try to find a reduced failing input set for a crashing Windows stage."""
    current = list(abs_files)
    attempts: List[dict] = []
    if len(current) <= 1:
        return current, attempts

    changed = True
    while changed and len(current) > 1:
        changed = False
        i = 0
        while i < len(current) and len(current) > 1:
            removed = current[i]
            trial = current[:i] + current[i + 1 :]
            trial_args = _build_slang_dump_args(
                tool=tool,
                top=top,
                extra_args=normalized_extra,
                abs_files=trial,
                stage=stage,
            )
            trial_proc = _run_slang_dump(trial_args, run_env)
            crash = _is_windows_access_violation(int(trial_proc.returncode))
            attempts.append(
                {
                    "removed_file": removed,
                    "trial_file_count": len(trial),
                    "return_code": int(trial_proc.returncode),
                    "kept_reduction": bool(crash),
                }
            )
            if crash:
                current = trial
                changed = True
            else:
                i += 1
    return current, attempts


def _run_windows_stage_diagnostics(
    tool: Path,
    top: str,
    normalized_extra: Sequence[str],
    abs_files: Sequence[str],
    run_env: Dict[str, str],
    runtime_snapshot: Optional[Dict[str, object]] = None,
    prefix_toolchain_meta: Optional[Dict[str, object]] = None,
) -> Tuple[List[str], Optional[List[str]], str, Optional[Path]]:
    """Probe staged slang_dump execution on Windows to isolate crash stage."""
    stage_results: List[dict] = []
    log_lines: List[str] = []
    last_ok_stage = ""
    first_fail_stage = ""
    first_fail_code = 0
    minimized_inputs: List[str] = []
    minimize_attempts: List[dict] = []

    for stage in _WINDOWS_SVVIEW_STAGES:
        stage_args = _build_slang_dump_args(
            tool=tool,
            top=top,
            extra_args=normalized_extra,
            abs_files=abs_files,
            stage=stage,
        )
        proc = _run_slang_dump(stage_args, run_env)
        stderr = (proc.stderr or "").strip()
        entry = {
            "stage": stage,
            "return_code": int(proc.returncode),
            "stderr_head": (stderr.splitlines()[0].strip() if stderr else ""),
            "stdout_lines": len((proc.stdout or "").splitlines()),
        }
        stage_results.append(entry)
        if proc.returncode == 0:
            last_ok_stage = stage
            continue
        first_fail_stage = stage
        first_fail_code = int(proc.returncode)
        break

    if (
        first_fail_stage
        and _is_windows_access_violation(first_fail_code)
        and len(abs_files) > 1
    ):
        minimized_inputs, minimize_attempts = _minimize_windows_crash_inputs(
            tool=tool,
            top=top,
            normalized_extra=normalized_extra,
            abs_files=abs_files,
            run_env=run_env,
            stage=first_fail_stage,
        )

    payload = {
        "generated_at_unix_sec": float(time.time()),
        "tool": str(tool),
        "top": str(top or ""),
        "extra_args": list(normalized_extra),
        "input_files": list(abs_files),
        "stage_results": stage_results,
        "last_ok_stage": last_ok_stage,
        "first_fail_stage": first_fail_stage,
        "first_fail_code": first_fail_code,
        "runtime_snapshot": dict(runtime_snapshot or {}),
        "prefix_toolchain_meta": dict(prefix_toolchain_meta or {}),
    }
    if minimized_inputs:
        payload["failing_input_minset"] = minimized_inputs
        payload["failing_input_minset_count"] = len(minimized_inputs)
        payload["failing_input_minset_attempts"] = minimize_attempts
    diag_path = _write_windows_stage_diagnostics(_repo_root(), payload)
    log_lines.append("[rtlens] windows stage diagnostics")
    if first_fail_stage:
        log_lines.append(
            f"first failing stage: {first_fail_stage} (return code={first_fail_code})"
        )
    else:
        log_lines.append("first failing stage: (none)")
    log_lines.append(f"last passing stage: {last_ok_stage or '(none)'}")
    if minimized_inputs:
        log_lines.append(
            "windows crash input-minset: "
            f"{len(minimized_inputs)}/{len(abs_files)} files"
        )
    if diag_path:
        log_lines.append(f"diagnostics json: {diag_path}")

    safe_stage = ""
    safe_args: Optional[List[str]] = None
    if first_fail_stage and last_ok_stage and last_ok_stage != "full":
        safe_stage = last_ok_stage
        safe_args = _build_slang_dump_args(
            tool=tool,
            top=top,
            extra_args=normalized_extra,
            abs_files=abs_files,
            stage=safe_stage,
        )
    return log_lines, safe_args, safe_stage, diag_path


def _detect_slang_config(prefix: Path) -> Optional[Path]:
    """Detect a usable ``slangConfig.cmake`` under the given install prefix."""
    for rel in (
        Path("lib/cmake/slang/slangConfig.cmake"),
        Path("lib64/cmake/slang/slangConfig.cmake"),
    ):
        p = prefix / rel
        if p.exists():
            return p
    return None


def _detect_svlang_library(prefix: Path) -> Optional[Path]:
    """Detect a usable ``libsvlang`` under the given install prefix."""
    patterns = [
        "libsvlang.a",
        "libsvlang.so",
        "libsvlang.dylib",
        "svlang.lib",
        "svlang.dll",
    ]
    for libdir in (prefix / "lib", prefix / "lib64", prefix / "bin"):
        if not libdir.exists():
            continue
        for name in patterns:
            p = libdir / name
            if p.exists():
                return p
    return None


def _resolve_standalone_slang_prefix(root: Path) -> Path:
    """Resolve standalone slang install-prefix used for ``slang_dump`` build.

    Search order:
    1. ``RTLENS_SLANG_ROOT`` environment variable (if set)
    2. ``<repo-root>/.deps/slang``

    Returns:
        Existing prefix that contains include files, cmake config, and
        ``libsvlang`` artifacts.

    Raises:
        SlangBackendError: If no usable standalone slang install-prefix is found.
    """
    env_value = str(os.environ.get("RTLENS_SLANG_ROOT", "") or "").strip()
    candidates: List[Path] = []
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.append(root / ".deps" / "slang")

    required_header = Path("include/slang/ast/ASTVisitor.h")
    missing_report: List[str] = []
    for base in _unique_paths(candidates):
        missing: List[str] = []
        if not (base / required_header).exists():
            missing.append(str(base / required_header))
        if _detect_slang_config(base) is None:
            missing.append(str(base / "lib/cmake/slang/slangConfig.cmake"))
            missing.append(str(base / "lib64/cmake/slang/slangConfig.cmake"))
        if _detect_svlang_library(base) is None:
            missing.append(str(base / "lib/libsvlang.(a|so|dylib)"))
            missing.append(str(base / "lib64/libsvlang.(a|so|dylib)"))
            missing.append(str(base / "bin/svlang.(dll|lib)"))
        if not missing:
            return base
        missing_report.append(f"- {base}\n  missing:\n  - " + "\n  - ".join(missing))

    checked = "\n".join(missing_report) if missing_report else "(no candidates)"
    raise SlangBackendError(
        "failed to locate usable standalone slang install-prefix for slang_dump.\n"
        "Set RTLENS_SLANG_ROOT to your slang install prefix.\n"
        "Expected at minimum: include/slang/ast/ASTVisitor.h + slangConfig.cmake + libsvlang.\n"
        f"checked candidates:\n{checked}"
    )


def _build_with_standalone_slang(root: Path, slang_prefix: Path, src: Path, out: Path) -> str:
    """Build ``slang_dump`` via standalone slang install-prefix and CMake."""
    cmake = shutil.which("cmake")
    if not cmake:
        raise SlangBackendError(
            "failed to build slang_dump with standalone slang:\n"
            "required tool `cmake` was not found on PATH."
        )

    proj_dir = root / ".cache" / "slang_dump_standalone"
    src_dir = proj_dir / "src"
    build_dir = proj_dir / "build"
    src_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    src_q = str(src).replace("\\", "/")
    cmakelists = f"""cmake_minimum_required(VERSION 3.20)
project(rtlens_slang_dump LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(slang CONFIG REQUIRED)

include(CheckCXXSourceCompiles)
include(CheckLinkerFlag)
get_target_property(_slang_includes slang::slang INTERFACE_INCLUDE_DIRECTORIES)
set(CMAKE_REQUIRED_INCLUDES "${{_slang_includes}}")
set(CMAKE_REQUIRED_LIBRARIES slang::slang)
set(CMAKE_REQUIRED_FLAGS "-std=c++20")
check_cxx_source_compiles(
"#include <slang/ast/ASTVisitor.h>
using namespace slang::ast;
struct Probe : ASTVisitor<Probe, VisitFlags::Symbols> {{}};
int main() {{ Probe p; return 0; }}"
SVVIEW_HAVE_ASTVISITOR_FLAGS_API
)

add_executable(slang_dump "{src_q}")
if(SVVIEW_HAVE_ASTVISITOR_FLAGS_API)
  target_compile_definitions(slang_dump PRIVATE SVVIEW_SLANG_ASTVISITOR_FLAGS_API=1)
endif()
if(WIN32 AND CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  # MinGW Release optimization can intermittently crash in runtime AST traversal.
  target_compile_options(slang_dump PRIVATE -O0 -g)
  # Add static runtime flags only when supported by the active linker.
  check_linker_flag(CXX "-static-libgcc" SVVIEW_HAVE_STATIC_LIBGCC)
  if(SVVIEW_HAVE_STATIC_LIBGCC)
    target_link_options(slang_dump PRIVATE -static-libgcc)
  endif()
  check_linker_flag(CXX "-static-libstdc++" SVVIEW_HAVE_STATIC_LIBSTDCXX)
  if(SVVIEW_HAVE_STATIC_LIBSTDCXX)
    target_link_options(slang_dump PRIVATE -static-libstdc++)
  endif()
  check_linker_flag(CXX "-static-libwinpthread" SVVIEW_HAVE_STATIC_LIBWINPTHREAD)
  if(SVVIEW_HAVE_STATIC_LIBWINPTHREAD)
    target_link_options(slang_dump PRIVATE -static-libwinpthread)
  endif()
endif()
target_link_libraries(slang_dump PRIVATE slang::slang)
set_target_properties(slang_dump PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${{CMAKE_BINARY_DIR}}/bin")
"""
    (src_dir / "CMakeLists.txt").write_text(cmakelists, encoding="utf-8")

    prefix_meta = _read_slang_prefix_toolchain_meta(slang_prefix)

    def _toolchain_args() -> Tuple[List[str], str]:
        args: List[str] = []
        info_parts: List[str] = []

        fmt_dir = str(os.environ.get("RTLENS_FMT_DIR", "") or _meta_text(prefix_meta, "fmt_dir")).strip()
        if fmt_dir:
            args.append(f"-Dfmt_DIR={fmt_dir}")
            info_parts.append(f"fmt_dir={fmt_dir}")

        meta_cxx = _meta_text(prefix_meta, "cxx_compiler_resolved") or _meta_text(prefix_meta, "cxx_compiler")
        meta_cc = _meta_text(prefix_meta, "c_compiler_resolved") or _meta_text(prefix_meta, "c_compiler")
        env_cxx = str(os.environ.get("RTLENS_CXX_COMPILER", "") or "").strip()
        env_cc = str(os.environ.get("RTLENS_C_COMPILER", "") or "").strip()
        if env_cxx or meta_cxx:
            cxx = env_cxx or meta_cxx
            args.append(f"-DCMAKE_CXX_COMPILER={cxx}")
            info_parts.append(f"cxx={cxx}")
        if env_cc or meta_cc:
            cc = env_cc or meta_cc
            args.append(f"-DCMAKE_C_COMPILER={cc}")
            info_parts.append(f"cc={cc}")

        if os.name != "nt":
            return args, (" " + " ".join(info_parts) if info_parts else "")

        generator = str(os.environ.get("RTLENS_CMAKE_GENERATOR", "") or "").strip()
        c_compiler = env_cc
        cxx_compiler = env_cxx
        make_program = str(os.environ.get("RTLENS_CMAKE_MAKE_PROGRAM", "") or "").strip()
        if not generator:
            auto_make = shutil.which("mingw32-make")
            auto_ninja = shutil.which("ninja")
            auto_cxx = shutil.which("g++")
            auto_cc = shutil.which("gcc")
            auto_cl = shutil.which("cl")
            auto_nmake = shutil.which("nmake")
            if auto_make and auto_cc and auto_cxx:
                generator = "MinGW Makefiles"
                c_compiler = c_compiler or auto_cc
                cxx_compiler = cxx_compiler or auto_cxx
                make_program = make_program or auto_make
            elif auto_ninja and auto_cc and auto_cxx:
                generator = "Ninja"
                c_compiler = c_compiler or auto_cc
                cxx_compiler = cxx_compiler or auto_cxx
            elif auto_nmake and auto_cl:
                generator = "NMake Makefiles"
            else:
                raise SlangBackendError(
                    "failed to configure standalone slang build for slang_dump:\n"
                    "no usable CMake generator/toolchain found on Windows.\n"
                    "Install MSYS2 UCRT64 gcc + make "
                    "(mingw-w64-ucrt-x86_64-gcc, mingw-w64-ucrt-x86_64-make) "
                    "or set RTLENS_CMAKE_GENERATOR/RTLENS_CXX_COMPILER."
                )

        if generator:
            args.extend(["-G", generator])
        if c_compiler:
            args.append(f"-DCMAKE_C_COMPILER={c_compiler}")
        if cxx_compiler:
            args.append(f"-DCMAKE_CXX_COMPILER={cxx_compiler}")
        if make_program:
            args.append(f"-DCMAKE_MAKE_PROGRAM={make_program}")
        info = f" generator={generator or '(default)'}"
        if cxx_compiler:
            info += f" cxx={cxx_compiler}"
        if make_program:
            info += f" make={make_program}"
        if info_parts:
            info += " " + " ".join(info_parts)
        return args, info

    toolchain_args, toolchain_info_extra = _toolchain_args()
    configure_cmd = [
        cmake,
        "-S",
        str(src_dir),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_PREFIX_PATH={str(slang_prefix)}",
    ]
    configure_cmd.extend(toolchain_args)
    configure = subprocess.run(
        configure_cmd,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if configure.returncode != 0:
        raise SlangBackendError(
            "failed to configure standalone slang build for slang_dump:\n"
            f"{configure.stderr or configure.stdout}"
        )

    build_cmd = [cmake, "--build", str(build_dir), "--config", "Release"]
    build = subprocess.run(
        build_cmd,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if build.returncode != 0:
        detail = build.stderr or build.stdout
        if "fmt::v12::vformat[abi:cxx11]" in detail:
            detail += (
                "\n\nhint: this looks like a macOS C++ ABI mismatch between a GCC-built "
                "slang prefix and a clang/libc++ fmt library. Rebuild the slang prefix "
                "with rtlens/tools/setup_slang_prefix.py so the private GCC-built fmt "
                "prefix is recorded and reused for slang_dump."
            )
        raise SlangBackendError(
            "failed to build slang_dump with standalone slang:\n"
            f"{detail}"
        )

    exe_names = ["slang_dump", "slang_dump.exe"]
    candidates: List[Path] = []
    for name in exe_names:
        candidates.extend(
            [
                build_dir / "bin" / name,
                build_dir / "bin" / "Release" / name,
                build_dir / "Release" / name,
                build_dir / name,
            ]
        )
    built = next((p for p in candidates if p.exists()), None)
    if not built:
        checked = "\n".join(str(p) for p in candidates)
        raise SlangBackendError(
            "standalone slang build completed but slang_dump binary was not found.\n"
            f"checked paths:\n{checked}"
        )
    shutil.copy2(str(built), str(out))
    os.chmod(str(out), 0o755)
    return f"standalone slang prefix={slang_prefix}{toolchain_info_extra} [{_CMAKELISTS_RECIPE_TAG}]"


def _ensure_tool() -> Tuple[Path, str]:
    """Build the ``slang_dump`` binary if it is missing or out of date.

    Build strategy:
    1. standalone slang install-prefix via CMake ``find_package(slang)``.

    Returns:
        Tuple of ready-to-run ``slang_dump`` binary path and toolchain info.

    Raises:
        SlangBackendError: If standalone build path fails.
    """
    src = _tool_src()
    out = _tool_bin()
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        meta = _read_tool_meta()
        if _CMAKELISTS_RECIPE_TAG in meta:
            return out, meta
        # Build recipe has changed; fall through to rebuild.

    root = _repo_root()
    slang_prefix = _resolve_standalone_slang_prefix(root)
    toolchain_info = _build_with_standalone_slang(root, slang_prefix, src, out)
    _write_tool_meta(toolchain_info)
    return out, toolchain_info


def _unesc(s: str) -> str:
    """Unescape tab, newline, and backslash sequences in a slang_dump token."""
    return s.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")


def _hier_parent(path: str) -> Optional[str]:
    """Return the parent hierarchy path, or ``None`` for a root node."""
    p = path.rfind(".")
    if p < 0:
        return None
    return path[:p]


def load_design_with_slang(
    files: Iterable[str], top: str = "", extra_args: Optional[List[str]] = None
) -> Tuple[DesignDB, ConnectivityDB, str]:
    """Elaborate a SystemVerilog design using the slang backend.

    Invokes the ``slang_dump`` binary (built on demand) on the given source
    files and parses its tab-separated output into a pair of databases.

    Args:
        files: Iterable of ``.sv`` / ``.v`` source file paths. All paths are
            converted to absolute before being passed to the subprocess.
        top: Optional top-module name.  Passed as ``--rtlens-top <top>`` to
            ``slang_dump`` when non-empty.
        extra_args: Additional flags forwarded verbatim to ``slang_dump``
            (e.g. ``["+incdir+src", "-DSIM"]``).

    Returns:
        Tuple ``(db, cdb, log)`` where

        * *db* is a :class:`~rtlens.model.DesignDB` with module definitions,
          hierarchy nodes, and callable (function/task/module) cross-references.
        * *cdb* is a :class:`~rtlens.model.ConnectivityDB` with signal-level
          driver/load sites and data-flow edges.
        * *log* is a multi-line diagnostic string suitable for display in a
          log panel.

    Raises:
        SlangBackendError: If the binary cannot be built or returns a non-zero
            exit code.
    """
    tool, toolchain_info = _ensure_tool()
    cwd = os.getcwd()

    args = [str(tool)]
    if top:
        args.extend(["--rtlens-top", top])
    normalized_extra = _normalize_windows_slang_args(list(extra_args or []))
    abs_files = [os.path.abspath(f) for f in files]
    if os.name == "nt":
        abs_files = [_normalize_windows_path_text(f) for f in abs_files]
    args = _build_slang_dump_args(
        tool=tool,
        top=top,
        extra_args=normalized_extra,
        abs_files=abs_files,
    )

    run_env = os.environ.copy()
    runtime_path_prepend: List[str] = []
    runtime_snapshot: Dict[str, object] = {}
    prefix_toolchain_meta: Dict[str, object] = {}
    mismatch_lines: List[str] = []
    if os.name == "nt":
        crash_key = _windows_crash_key(tool, top, abs_files)
        if _windows_crash_guard_enabled() and crash_key in _WINDOWS_NATIVE_CRASH_CACHE:
            cached = _WINDOWS_NATIVE_CRASH_CACHE.get(crash_key, "").strip()
            msg_lines = [
                "[rtlens] slang backend",
                "windows guard: skipped slang_dump after previous native crash in this session.",
            ]
            if cached:
                msg_lines.append(f"previous crash: {cached}")
            msg_lines.append(
                "hint: set RTLENS_WINDOWS_SLANG_RETRY=1 to bypass the session guard once."
            )
            raise SlangBackendError("\n".join(msg_lines))
        runtime_path_prepend = _prepend_env_path(
            run_env,
            _windows_runtime_path_candidates(tool, _repo_root()),
        )
        runtime_snapshot = _collect_windows_runtime_snapshot(
            tool=tool,
            run_env=run_env,
            runtime_path_prepend=runtime_path_prepend,
        )
        try:
            prefix = _resolve_standalone_slang_prefix(_repo_root())
        except SlangBackendError:
            prefix = None
        prefix_toolchain_meta = _read_slang_prefix_toolchain_meta(prefix)
        mismatch_lines = _windows_toolchain_mismatch_lines(
            prefix_toolchain_meta,
            runtime_snapshot,
        )

    proc = _run_slang_dump(args, run_env)
    retry_log_lines: List[str] = []
    diag_path: Optional[Path] = None
    if os.name == "nt" and _is_windows_access_violation(proc.returncode):
        fallback_extra = _drop_include_args(normalized_extra)
        if fallback_extra != normalized_extra:
            retry_args = _build_slang_dump_args(
                tool=tool,
                top=top,
                extra_args=fallback_extra,
                abs_files=abs_files,
            )
            retry = _run_slang_dump(retry_args, run_env)
            retry_log_lines.append("windows retry: native crash detected on first attempt")
            retry_log_lines.append("windows retry strategy: drop include-directory args (+incdir/-I)")
            retry_log_lines.append(
                f"retry command: {' '.join(shlex.quote(a) for a in retry_args)}"
            )
            retry_log_lines.append(f"retry return code: {retry.returncode}")
            if retry.returncode == 0:
                retry_log_lines.append("retry result: success")
                args = retry_args
                proc = retry
                normalized_extra = fallback_extra
            else:
                retry_stderr = retry.stderr.strip() or "(no stderr)"
                retry_log_lines.append("retry stderr:")
                retry_log_lines.append(retry_stderr)
        if _is_windows_access_violation(proc.returncode):
            diag_lines, safe_args, safe_stage, diag_path = _run_windows_stage_diagnostics(
                tool=tool,
                top=top,
                normalized_extra=normalized_extra,
                abs_files=abs_files,
                run_env=run_env,
                runtime_snapshot=runtime_snapshot,
                prefix_toolchain_meta=prefix_toolchain_meta,
            )
            retry_log_lines.extend(diag_lines)
            if safe_args:
                safe_proc = _run_slang_dump(safe_args, run_env)
                retry_log_lines.append(
                    f"windows stage fallback attempt: --rtlens-stage {safe_stage}"
                )
                retry_log_lines.append(f"stage fallback return code: {safe_proc.returncode}")
                if safe_proc.returncode == 0:
                    retry_log_lines.append("stage fallback result: success")
                    args = safe_args
                    proc = safe_proc
                else:
                    safe_stderr = safe_proc.stderr.strip() or "(no stderr)"
                    retry_log_lines.append("stage fallback stderr:")
                    retry_log_lines.append(safe_stderr)
    log_lines: List[str] = []
    log_lines.append("[rtlens] slang backend")
    log_lines.append(f"toolchain: {toolchain_info}")
    log_lines.append(f"command: {' '.join(shlex.quote(a) for a in args)}")
    log_lines.append(f"return code: {proc.returncode}")
    if runtime_path_prepend:
        log_lines.append(f"runtime PATH prepend count: {len(runtime_path_prepend)}")
        for p in runtime_path_prepend:
            log_lines.append(f"runtime PATH prepend: {p}")
    if mismatch_lines:
        log_lines.extend(mismatch_lines)
    log_lines.append(f"input files: {len(abs_files)}")
    log_lines.append(f"extra args: {len(normalized_extra)}")
    if retry_log_lines:
        log_lines.extend(retry_log_lines)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        log_lines.append("stderr:")
        log_lines.append(stderr)
        if os.name == "nt" and _is_windows_access_violation(proc.returncode):
            if _windows_crash_guard_enabled():
                reason = f"return_code={proc.returncode}"
                if diag_path:
                    reason += f" diag={diag_path}"
                _WINDOWS_NATIVE_CRASH_CACHE[crash_key] = reason
            log_lines.append(
                "hint: return code indicates native Windows crash (for example "
                "0xC0000005 / 0xC0000094); verify MinGW runtime DLL path and "
                "standalone slang prefix directories are available on PATH."
            )
            if diag_path:
                log_lines.append(
                    "hint: detailed Windows stage diagnostics were written to "
                    f"{diag_path}"
                )
        raise SlangBackendError("\n".join(log_lines))
    if os.name == "nt":
        _WINDOWS_NATIVE_CRASH_CACHE.pop(crash_key, None)

    db = DesignDB()
    cdb = ConnectivityDB()

    hier_temp: Dict[str, Tuple[str, str, int]] = {}
    children: Dict[str, List[str]] = defaultdict(list)

    tag_counts: Dict[str, int] = defaultdict(int)
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        parts = raw.split("\t")
        tag = parts[0]
        tag_counts[tag] += 1
        if tag == "H" and len(parts) >= 5:
            path = _unesc(parts[1])
            mod = _unesc(parts[2])
            file = _unesc(parts[3])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[4]) if parts[4].isdigit() else 1
            hier_temp[path] = (mod, file, line)
        elif tag == "S" and len(parts) >= 5:
            sig = _unesc(parts[1])
            file = _unesc(parts[3])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[4]) if parts[4].isdigit() else 1
            cdb.signal_to_source[sig] = SourceLoc(file=file, line=line)
            cdb.drives_data.setdefault(sig, set())
            cdb.drives_control.setdefault(sig, set())
        elif tag in {"D", "DP", "LD", "LP", "LC"} and len(parts) >= 4:
            sig = _unesc(parts[1])
            file = _unesc(parts[2])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[3]) if parts[3].isdigit() else 1
            loc = SourceLoc(file=file, line=line)
            if tag == "D":
                cdb.add_driver_site(sig, loc)
            elif tag == "DP":
                cdb.add_driver_site_port(sig, loc)
            elif tag == "LC":
                cdb.add_load_site(sig, loc, kind="control")
            elif tag == "LP":
                cdb.add_load_site(sig, loc, kind="port")
            else:
                cdb.add_load_site(sig, loc, kind="data")
            cdb.signal_to_source.setdefault(sig, loc)
            cdb.drives_data.setdefault(sig, set())
            cdb.drives_control.setdefault(sig, set())
        elif tag in {"E", "ED", "EC", "EP"} and len(parts) >= 5:
            src = _unesc(parts[1])
            dst = _unesc(parts[2])
            file = _unesc(parts[3])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[4]) if parts[4].isdigit() else 1
            kind = "control" if tag == "EC" else "data"
            cdb.add_edge(src, dst, kind=kind)
            if tag == "EP":
                cdb.add_alias(src, dst)
            cdb.signal_to_source.setdefault(src, SourceLoc(file=file, line=line))
            cdb.signal_to_source.setdefault(dst, SourceLoc(file=file, line=line))
        elif tag == "MD" and len(parts) >= 5:
            kind = _unesc(parts[1]).strip() or "module"
            name = _unesc(parts[2])
            file = _unesc(parts[3])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[4]) if parts[4].isdigit() else 1
            key = f"{kind}:{name}"
            db.callable_defs[key] = SourceLoc(file=file, line=line)
            db.callable_kinds[key] = kind
            db.callable_names[key] = name
            db.callable_name_index.setdefault(name, set()).add(key)
            db.callable_def_sites.setdefault((file, line, name), []).append(key)
        elif tag == "SD" and len(parts) >= 6:
            kind = _unesc(parts[1]).strip() or "function"
            full_name = _unesc(parts[2])
            name = _unesc(parts[3])
            file = _unesc(parts[4])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[5]) if parts[5].isdigit() else 1
            key = f"{kind}:{full_name}"
            db.callable_defs[key] = SourceLoc(file=file, line=line)
            db.callable_kinds[key] = kind
            db.callable_names[key] = name
            db.callable_name_index.setdefault(name, set()).add(key)
            db.callable_def_sites.setdefault((file, line, name), []).append(key)
            db.callable_def_sites.setdefault((file, line, full_name), []).append(key)
        elif tag == "MR" and len(parts) >= 6:
            # module reference: MR module <target_module> <token(instance)> <file> <line>
            kind = _unesc(parts[1]).strip() or "module"
            target = _unesc(parts[2])
            token = _unesc(parts[3])
            file = _unesc(parts[4])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[5]) if parts[5].isdigit() else 1
            key = f"{kind}:{target}"
            loc = SourceLoc(file=file, line=line)
            db.callable_refs.setdefault(key, []).append(loc)
            db.callable_ref_sites.setdefault((file, line, token), []).append(key)
            db.callable_ref_sites.setdefault((file, line, target), []).append(key)
        elif tag == "SR" and len(parts) >= 6:
            # subroutine reference: SR function|task <target_full> <token> <file> <line>
            kind = _unesc(parts[1]).strip() or "function"
            target = _unesc(parts[2])
            token = _unesc(parts[3])
            file = _unesc(parts[4])
            if file and not os.path.isabs(file):
                file = os.path.abspath(os.path.join(cwd, file))
            line = int(parts[5]) if parts[5].isdigit() else 1
            key = f"{kind}:{target}"
            loc = SourceLoc(file=file, line=line)
            db.callable_refs.setdefault(key, []).append(loc)
            db.callable_ref_sites.setdefault((file, line, token), []).append(key)
            db.callable_ref_sites.setdefault((file, line, target), []).append(key)
            short_name = target.split("::")[-1].split(".")[-1]
            db.callable_ref_sites.setdefault((file, line, short_name), []).append(key)

    for path, (mod, _file, _line) in hier_temp.items():
        parent = _hier_parent(path)
        db.hier[path] = HierNode(path=path, module_name=mod, inst_name=path.split(".")[-1], parent=parent, children=[])

    for path in db.hier.keys():
        parent = db.hier[path].parent
        if parent and parent in db.hier:
            children[parent].append(path)

    for parent, ch in children.items():
        db.hier[parent].children = sorted(ch)

    roots = [p for p, n in db.hier.items() if n.parent is None]
    db.roots = sorted(roots)
    db.top_module = db.hier[db.roots[0]].module_name if db.roots else None

    for path, (_mod, file, line) in hier_temp.items():
        mod_name = db.hier[path].module_name
        if mod_name not in db.modules:
            from .model import ModuleDef

            db.modules[mod_name] = ModuleDef(name=mod_name, file=file, start_line=line, end_line=line)

    # Keep references deterministic and unique.
    for key, refs in list(db.callable_refs.items()):
        seen: Set[Tuple[str, int]] = set()
        uniq: List[SourceLoc] = []
        for r in refs:
            k = (r.file, r.line)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        db.callable_refs[key] = sorted(uniq, key=lambda x: (x.file, x.line))
    for k, vals in list(db.callable_ref_sites.items()):
        db.callable_ref_sites[k] = sorted(set(vals))
    for k, vals in list(db.callable_def_sites.items()):
        db.callable_def_sites[k] = sorted(set(vals))

    log_lines.append("tag counts:")
    for k in sorted(tag_counts.keys()):
        log_lines.append(f"  {k}: {tag_counts[k]}")
    stderr = proc.stderr.strip()
    log_lines.append("stderr:")
    log_lines.append(stderr if stderr else "(none)")
    log_lines.append(
        f"summary: modules={len(db.modules)} hier_nodes={len(db.hier)} "
        f"signals={len(cdb.signal_to_source)}"
    )
    return db, cdb, "\n".join(log_lines)
