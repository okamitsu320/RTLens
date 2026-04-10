#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Iterable, List, Optional


REPO = Path(__file__).resolve().parents[2]


def _is_venv_python() -> bool:
    return Path(sys.prefix).resolve() != Path(getattr(sys, "base_prefix", sys.prefix)).resolve()


def _default_venv() -> Path:
    env = os.environ.get("VIRTUAL_ENV", "").strip()
    if env:
        return Path(env).expanduser()
    if _is_venv_python():
        return Path(sys.prefix)
    return REPO / ".venv"


def _path_flags(path: Path) -> int:
    try:
        return int(getattr(path.lstat(), "st_flags", 0))
    except OSError:
        return 0


def _is_hidden(path: Path) -> bool:
    return bool(_path_flags(path) & getattr(stat, "UF_HIDDEN", 0))


def _clear_hidden_flag(path: Path) -> bool:
    if not hasattr(os, "chflags"):
        return False
    flags = _path_flags(path)
    hidden = getattr(stat, "UF_HIDDEN", 0)
    if not flags & hidden:
        return False
    try:
        os.chflags(path, flags & ~hidden, follow_symlinks=False)
    except TypeError:
        os.chflags(path, flags & ~hidden)
    return True


def _walk_existing_roots(roots: Iterable[Path]) -> Iterable[Path]:
    seen = set()
    for root in roots:
        root = root.resolve()
        if not root.exists():
            continue
        for base, dirs, files in os.walk(root):
            base_path = Path(base)
            candidates = [base_path]
            candidates.extend(base_path / name for name in dirs)
            candidates.extend(base_path / name for name in files)
            for path in candidates:
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                yield path


def _pyside6_paths() -> tuple[Optional[Path], Optional[Path], List[Path], str]:
    try:
        import PySide6  # type: ignore
        from PySide6.QtCore import QLibraryInfo  # type: ignore
    except Exception as exc:
        return None, None, [], f"PySide6 import failed: {exc}"

    package_dir = Path(PySide6.__file__).resolve().parent
    try:
        plugins_dir = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    except Exception:
        plugins_dir = package_dir / "Qt" / "plugins"

    platforms_dir = plugins_dir / "platforms"
    platform_plugins = sorted(platforms_dir.glob("*.dylib")) if platforms_dir.is_dir() else []
    return package_dir, plugins_dir, platform_plugins, ""


def _print_check(venv: Path) -> int:
    package_dir, plugins_dir, platform_plugins, error = _pyside6_paths()

    print("# RTLens macOS Qt plugin check")
    print(f"platform: {sys.platform}")
    print(f"python: {sys.executable}")
    print(f"venv: {venv}")
    print(f"VIRTUAL_ENV: {os.environ.get('VIRTUAL_ENV', '')}")
    print(f"QT_PLUGIN_PATH: {os.environ.get('QT_PLUGIN_PATH', '')}")
    print(f"QT_QPA_PLATFORM_PLUGIN_PATH: {os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH', '')}")
    print(f"QT_QPA_PLATFORM: {os.environ.get('QT_QPA_PLATFORM', '')}")

    if error:
        print(f"[MISS] {error}")
        return 1

    assert package_dir is not None
    assert plugins_dir is not None
    print(f"PySide6 package: {package_dir}")
    print(f"Qt plugins: {plugins_dir}")

    interesting = [venv, package_dir, plugins_dir, plugins_dir / "platforms"]
    interesting.extend(platform_plugins)
    hidden = [p for p in interesting if p.exists() and _is_hidden(p)]

    if platform_plugins:
        print("platform plugins:")
        for plugin in platform_plugins:
            tag = " hidden" if _is_hidden(plugin) else ""
            print(f"  - {plugin.name}{tag}")
    else:
        print("[MISS] no platform plugin dylibs found")
        return 1

    if hidden:
        print("[WARN] macOS hidden flag is present on Qt/PySide6 paths:")
        for path in hidden:
            print(f"  - {path}")
        print("hint: run this tool with `--fix-hidden-flags --venv .venv`")
        return 2

    print("[OK] no macOS hidden flags found on checked Qt/PySide6 paths")
    return 0


def _fix_hidden_flags(venv: Path) -> int:
    if sys.platform != "darwin":
        print("[SKIP] --fix-hidden-flags is only supported on macOS")
        return 0
    if not venv.exists():
        print(f"[MISS] venv path does not exist: {venv}")
        return 1
    if not hasattr(os, "chflags"):
        print("[MISS] os.chflags is not available in this Python build")
        return 1

    package_dir, plugins_dir, _platform_plugins, error = _pyside6_paths()
    roots: List[Path] = [venv]
    if package_dir and package_dir.exists():
        roots.append(package_dir)
    if plugins_dir and plugins_dir.exists():
        roots.append(plugins_dir)

    changed = 0
    checked = 0
    for path in _walk_existing_roots(roots):
        checked += 1
        if _clear_hidden_flag(path):
            changed += 1

    print("# RTLens macOS Qt plugin hidden-flag fix")
    print(f"venv: {venv}")
    if error:
        print(f"[WARN] {error}")
    print(f"checked: {checked}")
    print(f"changed: {changed}")
    return _print_check(venv)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose and temporarily fix macOS PySide6 Qt plugin hidden flags")
    parser.add_argument("--venv", default="", help="venv directory to inspect or fix; default is active venv or .venv")
    parser.add_argument("--check", action="store_true", help="print PySide6/Qt plugin diagnostics")
    parser.add_argument("--fix-hidden-flags", action="store_true", help="clear macOS hidden flags below the venv")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    venv = Path(args.venv).expanduser() if args.venv else _default_venv()

    if sys.platform != "darwin":
        print("[SKIP] this tool is intended for macOS PySide6/QPA plugin diagnostics")
        return 0

    if args.fix_hidden_flags:
        return _fix_hidden_flags(venv)
    return _print_check(venv)


if __name__ == "__main__":
    raise SystemExit(main())
