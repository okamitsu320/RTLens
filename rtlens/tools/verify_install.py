#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import string
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


REPO = Path(__file__).resolve().parents[2]
if sys.platform.startswith("win"):
    DEFAULT_EDITOR_TEMPLATE = "notepad {fileq}"
elif sys.platform == "darwin":
    DEFAULT_EDITOR_TEMPLATE = "open -a TextEdit {fileq}"
else:
    DEFAULT_EDITOR_TEMPLATE = "xdg-open {fileq}"
_ALLOWED_EDITOR_FIELDS = {"file", "fileq", "line"}


@dataclass
class CheckResult:
    name: str
    status: str  # ok | missing | warn
    required: bool
    detail: str
    hint: str = ""


def _host_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "unknown"


def _ok(name: str, required: bool, detail: str) -> CheckResult:
    return CheckResult(name=name, status="ok", required=required, detail=detail)


def _missing(name: str, required: bool, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(name=name, status="missing", required=required, detail=detail, hint=hint)


def _warn(name: str, required: bool, detail: str, hint: str = "") -> CheckResult:
    return CheckResult(name=name, status="warn", required=required, detail=detail, hint=hint)


def _check_python() -> CheckResult:
    vi = sys.version_info
    got = f"{vi.major}.{vi.minor}.{vi.micro}"
    if (vi.major, vi.minor) < (3, 10):
        return _missing("python", True, f"requires >=3.10, found {got}")
    return _ok("python", True, got)


def _check_pyside6() -> CheckResult:
    try:
        import PySide6  # type: ignore

        return _ok("PySide6", True, getattr(PySide6, "__version__", "installed"))
    except Exception as e:
        return _missing(
            "PySide6",
            True,
            f"import failed: {e}",
            hint=f"install with `{_venv_python_hint()} -m pip install -e .`",
        )


def _has_macos_hidden_flag(path: Path) -> bool:
    try:
        flags = getattr(path.lstat(), "st_flags", 0)
    except OSError:
        return False
    return bool(flags & getattr(stat, "UF_HIDDEN", 0))


def _check_pyside6_qpa_plugins(target_os: str, host_os: str) -> CheckResult:
    if target_os != "mac":
        return _ok("PySide6_qpa_plugins", False, "not applicable for target OS")

    try:
        import PySide6  # type: ignore
        from PySide6.QtCore import QLibraryInfo  # type: ignore
    except Exception as e:
        return _warn(
            "PySide6_qpa_plugins",
            False,
            f"PySide6 QtCore import failed: {e}",
            hint=f"install with `{_venv_python_hint()} -m pip install -e .`",
        )

    try:
        plugins_root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    except Exception:
        plugins_root = Path(PySide6.__file__).resolve().parent / "Qt" / "plugins"

    platforms_dir = plugins_root / "platforms"
    cocoa = platforms_dir / "libqcocoa.dylib"
    if not cocoa.is_file():
        return _missing(
            "PySide6_qpa_plugins",
            False,
            f"missing macOS Qt platform plugin: {cocoa}",
            hint="reinstall PySide6 in the active venv",
        )

    if host_os != "mac":
        return _warn(
            "PySide6_qpa_plugins",
            False,
            f"target_os=mac, host_os={host_os}: hidden-flag check skipped; cocoa plugin exists at {cocoa}",
            hint="run verify_install on macOS for a real Qt plugin visibility check",
        )

    hidden_paths = [p for p in (plugins_root, platforms_dir, cocoa) if _has_macos_hidden_flag(p)]
    if hidden_paths:
        detail = "macOS hidden flag present on: " + ", ".join(str(p) for p in hidden_paths)
        return _warn(
            "PySide6_qpa_plugins",
            False,
            detail,
            hint=(
                "run `.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --check` and, "
                "if confirmed, `.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py "
                "--fix-hidden-flags --venv .venv`"
            ),
        )

    return _ok("PySide6_qpa_plugins", False, f"cocoa plugin visible at {cocoa}")


def _check_tkinter() -> CheckResult:
    try:
        import tkinter  # type: ignore  # noqa: F401

        return _ok("tkinter", False, "python tkinter module is available")
    except Exception as e:
        return _warn(
            "tkinter",
            False,
            f"import failed: {e}",
            hint="required only for `--ui tk` (install package such as `python3-tk`)",
        )


def _check_cmd(cmd: str, required: bool, hint: str = "") -> CheckResult:
    p = shutil.which(cmd)
    if p:
        return _ok(cmd, required, p)
    return _missing(cmd, required, "not found on PATH", hint=hint)


def _check_venv() -> CheckResult:
    virtual_env = str(os.environ.get("VIRTUAL_ENV", "") or "").strip()
    exe = Path(sys.executable)
    if virtual_env:
        return _ok("venv", False, f"{virtual_env} (python={exe})")
    if ".venv" in str(exe):
        return _ok("venv", False, str(exe))
    return _warn("venv", False, str(exe), hint=f"use `{_venv_python_hint()} ...` for reproducible checks")


def _venv_python_hint() -> str:
    if sys.platform.startswith("win"):
        return r".\.venv\Scripts\python.exe"
    return ".venv/bin/python"


def _build_editor_argv(template: str, file_path: str, line: int) -> List[str]:
    tpl = str(template or "").strip()
    if not tpl:
        raise ValueError("empty template")

    formatter = string.Formatter()
    for _literal, field_name, _format_spec, _conversion in formatter.parse(tpl):
        if field_name is None:
            continue
        if field_name not in _ALLOWED_EDITOR_FIELDS:
            raise ValueError(f"unsupported placeholder: {field_name}")

    mapping = {
        "file": shlex.quote(str(file_path)),
        "fileq": shlex.quote(str(file_path)),
        "line": int(line),
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


def _editor_hint(target_os: str) -> str:
    if target_os == "windows":
        return (
            "example: `--editor-cmd \"code --goto {file}:{line}\"` "
            "or set an absolute editor executable path."
        )
    if target_os == "mac":
        return (
            "example: `--editor-cmd \"code --goto {file}:{line}\"` "
            "or `--editor-cmd \"open -a TextEdit {fileq}\"`."
        )
    return (
        "example: `--editor-cmd \"xdg-open {fileq}\"` "
        "or `--editor-cmd \"code --goto {file}:{line}\"`."
    )


def _check_editor_cmd(template: str, target_os: str) -> CheckResult:
    sample = REPO / "rtlens" / "README.md"
    if not sample.is_file():
        sample = REPO
    try:
        argv = _build_editor_argv(template, str(sample), 1)
    except ValueError as exc:
        return _warn(
            "editor_cmd",
            False,
            f"template invalid: {exc}",
            hint=_editor_hint(target_os),
        )

    cmd0 = str(argv[0])
    found = ""
    p = Path(cmd0)
    if p.is_absolute():
        if p.is_file() and os.access(str(p), os.X_OK):
            found = str(p)
    elif any(sep in cmd0 for sep in ("/", "\\")):
        rp = (REPO / p).resolve() if not p.is_absolute() else p
        if rp.is_file() and os.access(str(rp), os.X_OK):
            found = str(rp)
    else:
        hit = shutil.which(cmd0)
        if hit:
            found = hit

    if found:
        return _ok("editor_cmd", False, f"{found} (template expands: {' '.join(argv)})")
    return _missing(
        "editor_cmd",
        False,
        f"editor executable not found: {cmd0}",
        hint=_editor_hint(target_os),
    )


def _slang_prefix_candidates() -> List[Path]:
    out: List[Path] = []
    env_root = str(os.environ.get("SVVIEW_SLANG_ROOT", "") or "").strip()
    if env_root:
        out.append(Path(env_root).expanduser())
    out.append(REPO / ".deps" / "slang")
    seen = set()
    uniq: List[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _detect_slang_config(prefix: Path) -> Optional[Path]:
    for rel in (
        Path("lib/cmake/slang/slangConfig.cmake"),
        Path("lib64/cmake/slang/slangConfig.cmake"),
    ):
        p = prefix / rel
        if p.is_file():
            return p
    return None


def _detect_svlang_lib(prefix: Path) -> Optional[Path]:
    names = ["libsvlang.a", "libsvlang.so", "libsvlang.dylib", "svlang.lib", "svlang.dll"]
    for d in (prefix / "lib", prefix / "lib64", prefix / "bin"):
        if not d.is_dir():
            continue
        for n in names:
            p = d / n
            if p.is_file():
                return p
    return None


def _read_slang_toolchain_meta(prefix: Path) -> dict:
    meta = prefix / "share" / "rtlens" / "slang_toolchain_meta.json"
    if not meta.is_file():
        return {}
    try:
        parsed = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_cmake_cache_value(cache: Path, key: str) -> str:
    if not cache.is_file():
        return ""
    prefix = f"{key}:"
    try:
        lines = cache.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for line in lines:
        if not line.startswith(prefix):
            continue
        _name_type, _sep, value = line.partition("=")
        return value.strip()
    return ""


def _looks_like_gnu_cxx(path_value: str) -> bool:
    name = Path(str(path_value or "")).name.lower()
    return name in {"g++", "gcc"} or name.startswith("g++-") or name.startswith("gcc-")


def _check_slang_artifacts() -> CheckResult:
    for cand in _slang_prefix_candidates():
        missing: List[str] = []
        hdr = cand / "include" / "slang" / "ast" / "ASTVisitor.h"
        if not hdr.is_file():
            missing.append(str(hdr))
        cfg = _detect_slang_config(cand)
        if cfg is None:
            missing.append(str(cand / "lib/cmake/slang/slangConfig.cmake"))
            missing.append(str(cand / "lib64/cmake/slang/slangConfig.cmake"))
        lib = _detect_svlang_lib(cand)
        if lib is None:
            missing.append(str(cand / "lib/libsvlang.(a|so|dylib)"))
            missing.append(str(cand / "lib64/libsvlang.(a|so|dylib)"))
            missing.append(str(cand / "bin/svlang.(lib|dll)"))
        if not missing:
            detail = str(cand)
            if cfg:
                detail += f" (config={cfg})"
            return _ok("slang_artifacts", True, detail)
    return _missing(
        "slang_artifacts",
        True,
        "standalone slang install-prefix not found in candidates",
        hint="set `SVVIEW_SLANG_ROOT=/path/to/slang-prefix` or run `rtlens/tools/setup_slang_prefix.py`",
    )


def _check_macos_slang_fmt_abi(target_os: str) -> CheckResult:
    if target_os != "mac":
        return _ok("macos_slang_fmt_abi", False, "not applicable for target OS")
    for cand in _slang_prefix_candidates():
        if _detect_slang_config(cand) is None or _detect_svlang_lib(cand) is None:
            continue
        meta = _read_slang_toolchain_meta(cand)
        cxx = str(
            meta.get("cxx_compiler_resolved")
            or meta.get("cxx_compiler")
            or _read_cmake_cache_value(REPO / ".cache" / "slang-build" / "CMakeCache.txt", "CMAKE_CXX_COMPILER")
            or ""
        )
        fmt_dir = str(
            meta.get("fmt_dir")
            or _read_cmake_cache_value(REPO / ".cache" / "slang-build" / "CMakeCache.txt", "fmt_DIR")
            or ""
        )
        system_fmt = not fmt_dir or fmt_dir.startswith("/usr/local/lib/cmake/fmt")
        if _looks_like_gnu_cxx(cxx) and system_fmt:
            return _warn(
                "macos_slang_fmt_abi",
                False,
                f"slang prefix appears to use GCC cxx={cxx or '(unknown)'} with system fmt_dir={fmt_dir or '(missing)'}",
                hint=(
                    "rebuild with `CC=/usr/local/opt/gcc/bin/gcc-15 CXX=/usr/local/opt/gcc/bin/g++-15 "
                    ".venv/bin/python rtlens/tools/setup_slang_prefix.py --clean --clone-if-missing "
                    "--slang-ref v10.0 --checkout-ref`"
                ),
            )
        detail = f"slang prefix={cand}"
        if cxx:
            detail += f" cxx={cxx}"
        if fmt_dir:
            detail += f" fmt_dir={fmt_dir}"
        return _ok("macos_slang_fmt_abi", False, detail)
    return _warn(
        "macos_slang_fmt_abi",
        False,
        "slang prefix metadata unavailable",
        hint="run setup_slang_prefix.py before GUI validation",
    )


def _check_elk_bundle() -> CheckResult:
    elk = REPO / "third_party" / "elk" / "node_modules" / "elkjs" / "lib" / "elk.bundled.js"
    if elk.is_file():
        return _ok("elkjs_bundle", True, str(elk))
    return _missing(
        "elkjs_bundle",
        True,
        "missing third_party/elk/node_modules/elkjs/lib/elk.bundled.js",
        hint=f"run `cd {REPO / 'third_party' / 'elk'} && npm ci`",
    )


def _yosys_hint(target_os: str) -> str:
    if target_os == "windows":
        return (
            "install via MSYS2 (for example `mingw-w64-x86_64-yosys`) "
            "and add the corresponding bin directory to PATH"
        )
    return "install yosys and ensure it is available on PATH"


def _check_os_opener(target_os: str, host: str) -> CheckResult:
    if target_os != host:
        return _warn(
            "desktop_opener",
            False,
            f"target_os={target_os}, host_os={host}: command check skipped",
            hint="run verify_install on the target OS for a real opener check",
        )
    if target_os == "linux":
        return _check_cmd("xdg-open", True, hint="install xdg-utils")
    if target_os == "mac":
        return _check_cmd("open", True)
    if target_os == "windows":
        return _ok("desktop_opener", True, "os.startfile available on Windows")
    return _warn("desktop_opener", False, f"unsupported target_os={target_os}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify RTLens runtime/install prerequisites")
    p.add_argument(
        "--target-os",
        default="auto",
        choices=["auto", "linux", "windows", "mac"],
        help="target OS profile for opener/tool checks",
    )
    p.add_argument("--strict", action="store_true", help="exit 1 when required checks are missing")
    p.add_argument(
        "--editor-cmd",
        default=DEFAULT_EDITOR_TEMPLATE,
        help="external editor argv template for preflight check ({file}/{fileq}/{line})",
    )
    p.add_argument("--json", action="store_true", help="print results as JSON")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    host = _host_os()
    target = host if args.target_os == "auto" else args.target_os

    results: List[CheckResult] = []
    results.append(_ok("repo_root", True, str(REPO)))
    results.append(_ok("host_os", True, host))
    results.append(_ok("target_os", True, target))
    results.append(_check_venv())
    results.append(_check_python())
    results.append(_check_pyside6())
    if target == "mac":
        results.append(_check_pyside6_qpa_plugins(target, host))
    results.append(_check_tkinter())
    results.append(_check_cmd("cmake", True, hint="install cmake (required for standalone slang build path)"))
    results.append(_check_cmd("g++", True, hint="install g++/build-essential"))
    results.append(_check_slang_artifacts())
    if target == "mac":
        results.append(_check_macos_slang_fmt_abi(target))
    results.append(_check_cmd("yosys", True, hint=_yosys_hint(target)))
    results.append(_check_cmd("node", True, hint="install Node.js"))
    results.append(_check_cmd("npm", True, hint="install npm"))
    results.append(_check_elk_bundle())
    results.append(_check_editor_cmd(args.editor_cmd, target))
    results.append(_check_os_opener(target, host))
    results.append(_check_cmd("dot", False, hint="optional; required for RTL Structure graphviz renderer"))
    results.append(_check_cmd("netlistsvg", False, hint="optional; install via npm"))
    results.append(_check_cmd("sv2v", False, hint="optional but recommended for complex SV"))
    results.append(_check_cmd("fst2vcd", False, hint="optional; required for .fst import"))
    results.append(_check_cmd("surfer", False, hint="optional external wave viewer bridge"))
    results.append(_check_cmd("gtkwave", False, hint="optional for .fst via fst2vcd"))

    strict_missing_required = [r for r in results if r.required and r.status != "ok"]

    if args.json:
        payload = {
            "host_os": host,
            "target_os": target,
            "strict": bool(args.strict),
            "results": [asdict(r) for r in results],
            "summary": {
                "ok": sum(1 for r in results if r.status == "ok"),
                "warn": sum(1 for r in results if r.status == "warn"),
                "missing": sum(1 for r in results if r.status == "missing"),
                "missing_required": len(strict_missing_required),
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"# rtlens verify_install host={host} target={target} strict={args.strict}")
        for r in results:
            tag = "OK" if r.status == "ok" else ("MISS" if r.status == "missing" else "WARN")
            req = " required" if r.required else ""
            print(f"[{tag}] {r.name}{req}: {r.detail}")
            if r.hint:
                print(f"       hint: {r.hint}")
        print(
            f"\nsummary: ok={sum(1 for r in results if r.status == 'ok')} "
            f"warn={sum(1 for r in results if r.status == 'warn')} "
            f"missing={sum(1 for r in results if r.status == 'missing')} "
            f"missing_required={len(strict_missing_required)}"
        )

    if args.strict and strict_missing_required:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
