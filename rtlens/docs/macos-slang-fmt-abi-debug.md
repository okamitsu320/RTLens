# macOS slang / fmt ABI Debug Notes

This note tracks a macOS-specific `slang_dump` build failure seen after the Qt
GUI starts successfully.

## Symptom

`slang_dump` fails to link:

```text
failed to build slang_dump with standalone slang:
Undefined symbols for architecture x86_64:
  "fmt::v12::vformat[abi:cxx11](fmt::v12::basic_string_view<char>, fmt::v12::basic_format_args<fmt::v12::context>)"
ld: symbol(s) not found for architecture x86_64
```

RTLens can then fall back to the lightweight parser, but the full slang-backed
hierarchy/connectivity path is unavailable.

## Cause

On macOS, the slang prefix may be built with Homebrew GCC:

```bash
export CC=/usr/local/opt/gcc/bin/gcc-15
export CXX=/usr/local/opt/gcc/bin/g++-15
```

If CMake resolves `fmt` to Homebrew's system package, the link can mix:

- GCC/libstdc++ objects from `libsvlang.a`
- clang/libc++ symbols from `/usr/local/lib/libfmt.*`

The GCC-built slang objects request `fmt::vformat[abi:cxx11]`, while the
Homebrew fmt library exports the ABI-tagless libc++ symbol.

## Recovery

Rebuild the slang prefix with the updated setup script. On macOS+GCC, it builds
a private GCC-compatible fmt prefix under `.deps/fmt-gcc` and records that
`fmt_DIR` in the slang metadata:

```bash
export CC=/usr/local/opt/gcc/bin/gcc-15
export CXX=/usr/local/opt/gcc/bin/g++-15
.venv/bin/python rtlens/tools/setup_slang_prefix.py --clean --clone-if-missing --slang-ref v10.0 --checkout-ref
```

Then force `slang_dump` to rebuild if an old binary exists:

```bash
rm -f rtlens/bin/slang_dump rtlens/bin/slang_dump.meta
```

Validation:

```bash
.venv/bin/python rtlens/tools/verify_install.py --target-os mac
.venv/bin/python -m rtlens --debug-callable --filelist RTL/verification/min_case/vlist --top vm_min_top
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

## Useful Checks

```bash
rg -n "CMAKE_(C|CXX)_COMPILER:|fmt_DIR" .cache/slang-build/CMakeCache.txt .cache/slang_dump_standalone/build/CMakeCache.txt
cat .deps/slang/share/rtlens/slang_toolchain_meta.json
find .deps/fmt-gcc -name 'fmt-config.cmake' -o -name 'fmtConfig.cmake'
```
