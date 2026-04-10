# macOS Fix Summary

This document summarizes the macOS fixes added while debugging RTLens on an
Intel Mac. It is intended as a handoff note for future maintainers or another
AI agent.

## Verified Environment

Observed local environment:

- CPU: Intel Mac (`x86_64`)
- Homebrew prefix: `/usr/local`
- Python venv: `.venv`
- GCC override used for slang:
  - `CC=/usr/local/opt/gcc/bin/gcc-15`
  - `CXX=/usr/local/opt/gcc/bin/g++-15`

The following recovery flow was confirmed by the user:

```bash
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --fix-hidden-flags --venv .venv
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

After rebuilding slang with the private fmt handling, the slang/fmt link issue
was also reported as resolved.

## Changed Files

| File | Type | Purpose |
|---|---|---|
| `rtlens/tools/macos_qt_plugin_fix.py` | New tool | Diagnoses PySide6 Qt plugin paths on macOS and clears macOS `hidden` flags under the venv. |
| `rtlens/docs/macos-pyside6-qpa-debug.md` | New doc | Records the PySide6/QPA `cocoa` plugin failure, diagnosis commands, and temporary recovery steps. |
| `rtlens/tools/verify_install.py` | Modified tool | Adds macOS checks for PySide6 `libqcocoa.dylib` visibility and slang/fmt ABI risk. |
| `rtlens/docs/install.md` | Modified doc | Adds macOS troubleshooting steps for PySide6 hidden flags and slang/fmt ABI failures. |
| `rtlens/tools/setup_slang_prefix.py` | Modified tool | Adds macOS+GCC private fmt setup, new fmt-related CLI options, and records compiler/fmt metadata. |
| `rtlens/rtlens/slang_backend.py` | Modified backend | Reuses the slang prefix compiler/fmt metadata when building `slang_dump`, and adds a targeted ABI-mismatch hint. |
| `rtlens/docs/macos-slang-fmt-abi-debug.md` | New doc | Records the `fmt::v12::vformat[abi:cxx11]` failure, root cause, and recovery flow. |
| `rtlens/docs/macos-fix-summary.md` | New doc | This summary file. |

## PySide6 / Qt QPA Fix

Problem:

- PySide6 imported successfully.
- `libqcocoa.dylib` existed under `.venv/lib/python*/site-packages/PySide6/Qt/plugins/platforms`.
- Qt still failed with:

```text
qt.qpa.plugin: Could not find the Qt platform plugin "cocoa" in ""
```

Observed cause:

- macOS `hidden` flags were present on `.venv` and PySide6 Qt plugin files.
- Qt's plugin loader could see the plugin directory but did not accept the
  platform plugin files as loadable candidates.

Added recovery:

```bash
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --check
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --fix-hidden-flags --venv .venv
```

Equivalent shell command:

```bash
chflags -R nohidden .venv
```

`verify_install.py --target-os mac` now warns when the PySide6 `cocoa` plugin or
its parent plugin directories are hidden.

## slang / fmt ABI Fix

Problem:

```text
Undefined symbols for architecture x86_64:
  "fmt::v12::vformat[abi:cxx11](fmt::v12::basic_string_view<char>, fmt::v12::basic_format_args<fmt::v12::context>)"
```

Observed cause:

- slang and `slang_dump` were built with Homebrew GCC (`g++-15`).
- CMake resolved `fmt` to Homebrew's system package under `/usr/local/lib/cmake/fmt`.
- The GCC-built slang archive required the libstdc++ ABI-tagged fmt symbol.
- Homebrew fmt exported the clang/libc++ ABI symbol instead.

Added recovery:

- On macOS with GCC, `setup_slang_prefix.py` builds a private fmt 12.1.0 prefix
  under `.deps/fmt-gcc`.
- slang is configured with that private `fmt_DIR`.
- `.deps/slang/share/rtlens/slang_toolchain_meta.json` records:
  - resolved C compiler
  - resolved C++ compiler
  - private `fmt_dir`
- `slang_backend.py` reuses that metadata when building `rtlens/bin/slang_dump`.

Expected rebuild flow:

```bash
export CC=/usr/local/opt/gcc/bin/gcc-15
export CXX=/usr/local/opt/gcc/bin/g++-15
.venv/bin/python rtlens/tools/setup_slang_prefix.py --clean --clone-if-missing --slang-ref v10.0 --checkout-ref
rm -f rtlens/bin/slang_dump rtlens/bin/slang_dump.meta
.venv/bin/python rtlens/tools/verify_install.py --target-os mac
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

## Apple Silicon Notes

The implementation is intended to be architecture-neutral where possible, but
only Intel Mac was verified in this debugging session.

Expected to work similarly:

- The PySide6 hidden-flag recovery should be independent of Intel vs Apple
  Silicon because it operates on macOS file flags.
- The slang/fmt fix is based on using one compiler family consistently for
  slang, private fmt, and `slang_dump`. That design should also apply to arm64.

Important Apple Silicon differences:

- Homebrew is usually installed under `/opt/homebrew`, not `/usr/local`.
- The Intel-specific compiler paths below should not be copied blindly:

```bash
export CC=/usr/local/opt/gcc/bin/gcc-15
export CXX=/usr/local/opt/gcc/bin/g++-15
```

Use `brew --prefix gcc` to derive the path instead:

```bash
GCC_PREFIX="$(brew --prefix gcc)"
export CC="${GCC_PREFIX}/bin/gcc-15"
export CXX="${GCC_PREFIX}/bin/g++-15"
```

If Homebrew installs a different major GCC version, adjust the suffix:

```bash
ls "$(brew --prefix gcc)/bin"/gcc-* "$(brew --prefix gcc)/bin"/g++-*
```

Known Apple Silicon risks:

- Homebrew GCC may have a different version suffix than `15`.
- PySide6 wheel availability and Qt plugin packaging can differ by Python
  version and arm64/x86_64 environment.
- slang v10.0 may expose a separate CMake, compiler, or source compatibility
  issue on arm64 that was not reproduced on this Intel Mac.
- Mixing Rosetta x86_64 tools with native arm64 Homebrew libraries is likely to
  fail. Keep Python, Homebrew packages, GCC, fmt, and slang on one architecture.

If Apple Silicon cannot use Homebrew GCC cleanly, the next fallback should be to
try a fully clang/libc++ build for slang and `slang_dump`, rather than mixing
GCC-built slang with Homebrew clang-built fmt.

## Handoff Checklist

Use these commands to inspect the macOS-specific state:

```bash
.venv/bin/python rtlens/tools/verify_install.py --target-os mac
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --check
cat .deps/slang/share/rtlens/slang_toolchain_meta.json
find .deps/fmt-gcc -name 'fmt-config.cmake' -o -name 'fmtConfig.cmake'
rg -n "CMAKE_(C|CXX)_COMPILER:|fmt_DIR" .cache/slang-build/CMakeCache.txt .cache/slang_dump_standalone/build/CMakeCache.txt
```

The desired post-fix state is:

- `PySide6_qpa_plugins` is `OK`.
- `macos_slang_fmt_abi` is `OK`.
- `slang_toolchain_meta.json` contains a private `.deps/fmt-gcc/.../cmake/fmt`
  `fmt_dir` when GCC is used on macOS.
- GUI regression can run the `mid_case` without falling back because of a
  `slang_dump` build failure.
