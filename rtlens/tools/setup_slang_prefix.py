#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
_TOOLCHAIN_META_REL = Path("share/rtlens/slang_toolchain_meta.json")


def _resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    return (REPO / p).resolve()


def _run(cmd: list[str], cwd: Path) -> None:
    print(f"[RUN] cwd={cwd} :: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")


def _norm_text(value: str) -> str:
    return str(value or "").strip()


def _resolve_executable(value: str) -> str:
    v = _norm_text(value)
    if not v:
        return ""
    found = shutil.which(v)
    if found:
        return str(Path(found))
    p = Path(v).expanduser()
    return str(p) if p.exists() else v


def _write_toolchain_meta(
    *,
    prefix: Path,
    args: argparse.Namespace,
    cmake_path: str,
    generator: str,
    c_compiler: str,
    cxx_compiler: str,
    make_program: str,
    slang_src: Path,
) -> Path:
    """Write standalone slang toolchain metadata under install prefix."""
    out = prefix / _TOOLCHAIN_META_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at_unix_sec": float(time.time()),
        "host_os": os.name,
        "prefix": str(prefix),
        "slang_source": str(slang_src),
        "requested_slang_ref": _norm_text(args.slang_ref),
        "build_type": _norm_text(args.build_type),
        "cmake": _resolve_executable(cmake_path),
        "cmake_generator": _norm_text(generator),
        "c_compiler": _resolve_executable(c_compiler),
        "cxx_compiler": _resolve_executable(cxx_compiler),
        "make_program": _resolve_executable(make_program),
        "cmake_cmd": _norm_text(args.cmake_cmd),
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build and install standalone slang prefix for RTLens")
    p.add_argument(
        "--slang-source",
        default="../slang",
        help="path to slang source tree (default: ../slang from repo root)",
    )
    p.add_argument(
        "--slang-repo-url",
        default="https://github.com/MikePopoloski/slang",
        help="slang git repository URL used with --clone-if-missing",
    )
    p.add_argument(
        "--slang-ref",
        default="v10.0",
        help="slang git ref (tag/branch/commit) used when cloning with --clone-if-missing",
    )
    p.add_argument(
        "--clone-if-missing",
        action="store_true",
        help="clone slang source automatically when --slang-source path is missing",
    )
    p.add_argument(
        "--checkout-ref",
        action="store_true",
        help="apply --slang-ref to an existing slang git checkout as well",
    )
    p.add_argument(
        "--build-dir",
        default=".cache/slang-build",
        help="cmake build directory relative to repo root",
    )
    p.add_argument(
        "--prefix",
        default=".deps/slang",
        help="install prefix relative to repo root",
    )
    p.add_argument(
        "--cmake-cmd",
        default="cmake",
        help="cmake executable name/path",
    )
    p.add_argument(
        "--cmake-generator",
        default="",
        help="cmake generator override (for example: Ninja, MinGW Makefiles, NMake Makefiles)",
    )
    p.add_argument("--c-compiler", default="", help="C compiler override passed to CMake")
    p.add_argument("--cxx-compiler", default="", help="C++ compiler override passed to CMake")
    p.add_argument("--make-program", default="", help="build tool override passed to CMake")
    p.add_argument("--build-type", default="Release", help="cmake build type")
    p.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1), help="parallel build jobs")
    p.add_argument("--clean", action="store_true", help="remove build dir before configure")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    cmake = shutil.which(args.cmake_cmd)
    if not cmake:
        print(f"[ERROR] cmake not found: {args.cmake_cmd}")
        return 2

    slang_src = _resolve_path(args.slang_source)
    build_dir = _resolve_path(args.build_dir)
    prefix = _resolve_path(args.prefix)
    git = shutil.which("git")

    if not (slang_src / "CMakeLists.txt").is_file():
        if not args.clone_if_missing:
            print(f"[ERROR] invalid slang source path: {slang_src}")
            print("        CMakeLists.txt not found")
            print("        hint: pass --clone-if-missing to auto-clone slang")
            return 2
        if not git:
            print("[ERROR] git is required for --clone-if-missing but not found on PATH")
            return 2
        clone_cmd = [git, "clone", "--depth", "1"]
        ref = str(args.slang_ref or "").strip()
        if ref:
            clone_cmd.extend(["--branch", ref])
        clone_cmd.extend([args.slang_repo_url, str(slang_src)])
        try:
            _run(clone_cmd, REPO)
        except RuntimeError as e:
            msg = str(e)
            fallback_allowed = bool(ref) and ("Remote branch" in msg or "not found in upstream origin" in msg)
            if not fallback_allowed:
                print(f"[ERROR] {e}")
                return 1
            print(f"[WARN] requested slang ref `{ref}` was not found. falling back to repository default branch.")
            clone_default = [git, "clone", "--depth", "1", args.slang_repo_url, str(slang_src)]
            try:
                _run(clone_default, REPO)
            except RuntimeError as e2:
                print(f"[ERROR] {e2}")
                return 1
    else:
        print(f"[INFO] using existing slang source: {slang_src}")
        ref = str(args.slang_ref or "").strip()
        if ref and args.checkout_ref:
            if not git or not (slang_src / ".git").is_dir():
                print("[ERROR] --checkout-ref requires a git checkout at --slang-source")
                return 2
            try:
                _run([git, "-C", str(slang_src), "fetch", "--tags", "--force", "--prune"], REPO)
                _run([git, "-C", str(slang_src), "checkout", ref], REPO)
            except RuntimeError as e:
                print(f"[ERROR] failed to checkout slang ref `{ref}`: {e}")
                return 1
        elif args.clone_if_missing and ref:
            print(
                "[INFO] --slang-ref is only applied when cloning unless --checkout-ref is set. "
                "Existing source tree is left unchanged."
            )

    if args.clean and build_dir.exists():
        shutil.rmtree(build_dir)

    build_dir.mkdir(parents=True, exist_ok=True)
    prefix.mkdir(parents=True, exist_ok=True)

    generator = str(args.cmake_generator or "").strip()
    c_compiler = str(args.c_compiler or "").strip()
    cxx_compiler = str(args.cxx_compiler or "").strip()
    make_program = str(args.make_program or "").strip()
    if os.name == "nt" and not generator:
        auto_make = shutil.which("mingw32-make")
        auto_ninja = shutil.which("ninja")
        auto_cxx = shutil.which("g++")
        auto_cc = shutil.which("gcc")
        auto_cl = shutil.which("cl")
        if auto_make and auto_cxx and auto_cc:
            generator = "MinGW Makefiles"
            c_compiler = c_compiler or auto_cc
            cxx_compiler = cxx_compiler or auto_cxx
            make_program = make_program or auto_make
        elif auto_ninja and auto_cxx and auto_cc:
            generator = "Ninja"
            c_compiler = c_compiler or auto_cc
            cxx_compiler = cxx_compiler or auto_cxx
        elif shutil.which("nmake") and auto_cl:
            generator = "NMake Makefiles"
        else:
            print("[ERROR] no usable CMake generator/toolchain found on Windows.")
            print("        install MSYS2 UCRT64 gcc + mingw32-make (package: mingw-w64-ucrt-x86_64-make),")
            print("        or provide --cmake-generator / compiler overrides explicitly.")
            return 2

    configure_cmd = [
        cmake,
        "-S",
        str(slang_src),
        "-B",
        str(build_dir),
        f"-DCMAKE_BUILD_TYPE={args.build_type}",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        "-DSLANG_INCLUDE_TOOLS=OFF",
        "-DSLANG_INCLUDE_TESTS=OFF",
        "-DSLANG_INCLUDE_DOCS=OFF",
        "-DSLANG_INCLUDE_PYLIB=OFF",
        "-DSLANG_INCLUDE_INSTALL=ON",
    ]
    if generator:
        configure_cmd.extend(["-G", generator])
    if c_compiler:
        configure_cmd.append(f"-DCMAKE_C_COMPILER={c_compiler}")
    if cxx_compiler:
        configure_cmd.append(f"-DCMAKE_CXX_COMPILER={cxx_compiler}")
    if make_program:
        configure_cmd.append(f"-DCMAKE_MAKE_PROGRAM={make_program}")
    if generator:
        print(f"[INFO] cmake generator: {generator}")
    if cxx_compiler:
        print(f"[INFO] cxx compiler: {cxx_compiler}")
    if make_program:
        print(f"[INFO] make program: {make_program}")
    build_cmd = [cmake, "--build", str(build_dir), "--config", args.build_type, "--parallel", str(args.jobs)]
    install_cmd = [cmake, "--install", str(build_dir), "--config", args.build_type]

    try:
        _run(configure_cmd, REPO)
        _run(build_cmd, REPO)
        _run(install_cmd, REPO)
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return 1

    meta_path = _write_toolchain_meta(
        prefix=prefix,
        args=args,
        cmake_path=cmake,
        generator=generator,
        c_compiler=c_compiler,
        cxx_compiler=cxx_compiler,
        make_program=make_program,
        slang_src=slang_src,
    )

    print("\n[OK] standalone slang prefix is ready")
    print(f"prefix: {prefix}")
    print(f"toolchain metadata: {meta_path}")
    print("Set this env var before running RTLens (if non-default path):")
    print(f"  export SVVIEW_SLANG_ROOT={prefix}")
    print("Quick check command (after creating .venv):")
    if os.name == "nt":
        print(r"  .\.venv\Scripts\python.exe rtlens/tools/verify_install.py --target-os windows --strict")
    else:
        print("  .venv/bin/python rtlens/tools/verify_install.py --target-os linux --strict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
