# RTLens Install Guide (Linux / Windows / macOS)

This guide is for repository-based setup and validation of RTLens.

Related docs:

- `README.md`
- `rtlens/docs/usage.md`

## 0. Acquire repository and move to repo root

If you start from outside the repository:

```bash
git clone <rtlens-repo-url> rtlens
cd rtlens
```

All commands below assume your current directory is this repository root.

## 1. Support policy

| OS | Support level | Release gate |
|---|---|---|
| Linux | primary | yes |
| Windows | best-effort | no |
| macOS | provisional reference | no |

Notes:

- Linux strict verification is required before release.
- Windows/macOS results are recorded for visibility, but do not block release.

## 2. Linux (primary baseline: Ubuntu 24.04)

### 2-A. Install required tools

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  build-essential cmake git \
  nodejs npm yosys graphviz xdg-utils
```

Optional but recommended:

```bash
npm install -g netlistsvg
```

### 2-B. Prepare standalone slang install-prefix

Recommended (auto clone by setup script):

```bash
python3 rtlens/tools/setup_slang_prefix.py \
  --clean \
  --clone-if-missing \
  --slang-ref v10.0 \
  --checkout-ref
```

If you use a custom prefix:

```bash
export RTLENS_SLANG_ROOT=/absolute/path/to/slang-prefix
```

Quick check:

```bash
test -f "${RTLENS_SLANG_ROOT:-.deps/slang}/include/slang/ast/ASTVisitor.h"
ls "${RTLENS_SLANG_ROOT:-.deps/slang}"/lib*/cmake/slang/slangConfig.cmake
```

### 2-C. Python environment and project setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e ".[dev]"

cd third_party/elk
npm ci
cd ../..
```

Activation is optional. This guide uses direct `.venv/bin/python ...` commands.

### 2-D. Verify and tests

```bash
# optional: force slang_dump build before GUI
.venv/bin/python -m rtlens --debug-callable --filelist RTL/verification/min_case/vlist --top vm_min_top

# required for Linux release gate
.venv/bin/python rtlens/tools/verify_install.py --target-os linux --strict
.venv/bin/python -m pytest -q rtlens/tests
```

Success criteria:

- `verify_install` exits `0`
- summary contains `missing_required=0`

### 2-E. GUI smoke

```bash
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case min_case
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case deep_case
```

Direct usage samples:

```bash
.venv/bin/python rtlens/tools/run_usage_samples.py --mode run --case mid_case
```

## 3. Windows (best-effort, Windows 11 + winget + MSYS2 UCRT64)

### 3-A. Install toolchain

Run in Administrator PowerShell:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Kitware.CMake -e
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e
winget install --id MSYS2.MSYS2 -e
winget install --id Graphviz.Graphviz -e
```

After `winget`, restart PowerShell once.

Start MSYS2 UCRT64 shell and install build tools:

```bash
pacman -Syu --noconfirm
pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-make
pacman -S --needed --noconfirm mingw-w64-x86_64-yosys
```

Back in PowerShell, set session PATH:

```powershell
$env:Path = "C:\msys64\ucrt64\bin;C:\msys64\mingw64\bin;$env:Path"
$env:Path = "C:\Program Files\nodejs;$env:Path"
$env:Path = "C:\Program Files\Graphviz\bin;$env:Path"
```

Verify commands:

```powershell
Get-Command python -ErrorAction SilentlyContinue
Get-Command py -ErrorAction SilentlyContinue
Get-Command cmake -ErrorAction SilentlyContinue
Get-Command g++ -ErrorAction SilentlyContinue
Get-Command mingw32-make -ErrorAction SilentlyContinue
Get-Command npm.cmd -ErrorAction SilentlyContinue
Get-Command yosys -ErrorAction SilentlyContinue
Get-Command dot -ErrorAction SilentlyContinue
```

### 3-B. Prepare slang / python environment

Python policy is `3.10+` (not pinned to only 3.12).

```powershell
py -3 --version
py -3 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Push-Location third_party/elk
npm.cmd ci
Pop-Location

.\.venv\Scripts\python.exe rtlens/tools/setup_slang_prefix.py --clean --clone-if-missing --slang-ref v10.0 --checkout-ref
```

If `py` is unavailable, use your full Python executable path.

### 3-C. Verify and smoke

```powershell
.\.venv\Scripts\python.exe rtlens/tools/verify_install.py --target-os windows
.\.venv\Scripts\python.exe -m pytest -q rtlens/tests
.\.venv\Scripts\python.exe rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

Windows known notes:

- Use `npm.cmd` in PowerShell (avoid `npm.ps1` execution-policy problems).
- If activation is blocked, skip activation and use `.\.venv\Scripts\python.exe ...`.
- If `pacman -Syu` fails intermittently, rerun and continue.

## 4. macOS (provisional reference only)

macOS setup is currently reference-only and is not a release gate.

### 4-A. Install Homebrew if missing

Check:

```bash
brew --version
```

If missing, install Homebrew from the official page:

- <https://brew.sh/>

### 4-B. Install tools (individual brew commands)

Run individually (more stable than one large install command on some systems):

```bash
brew update
brew install python@3.12
brew install cmake
brew install gcc
brew install node
brew install yosys
brew install graphviz
```

Optional:

```bash
brew install gtkwave
npm install -g netlistsvg
```

### 4-C. Provisional validation flow

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e ".[dev]"

cd third_party/elk && npm ci && cd ../..
python3 rtlens/tools/setup_slang_prefix.py --clean --clone-if-missing --slang-ref v10.0 --checkout-ref
.venv/bin/python rtlens/tools/verify_install.py --target-os mac
```

Known limitations and environment notes:

- Monterey 12.x can fail during slang build (`<source_location>` / toolchain issues).
- `gcc` on macOS resolves to Apple Clang, which has incomplete C++ standard library
  support (`<bit>`, `<any>`, `<array>` etc. may not be found). This is a known
  Apple Clang limitation, not a Command Line Tools breakage.
- **Fix**: use Homebrew GCC (real GCC, installed as `gcc-15` or similar).
  `brew --prefix gcc` resolves correctly on both Intel (`/usr/local/opt/gcc`)
  and Apple Silicon (`/opt/homebrew/opt/gcc`) Macs.

  ```bash
  # Step 1: find the installed version (number may change with future Homebrew updates)
  ls $(brew --prefix gcc)/bin/g++-*
  # e.g. output: .../g++-15

  # Step 2: set CXX/CC for the slang library build (adjust version number as shown above)
  export CXX=$(brew --prefix gcc)/bin/g++-15
  export CC=$(brew --prefix gcc)/bin/gcc-15

  # Step 3: rebuild slang (--clean required to discard the Apple Clang cache)
  python3 rtlens/tools/setup_slang_prefix.py --clean --slang-ref v10.0 --checkout-ref

  # Step 4: also set RTLENS_CXX_COMPILER for the slang_dump build inside RTLens
  export RTLENS_CXX_COMPILER=$(brew --prefix gcc)/bin/g++-15
  ```

  Note: the version suffix (`-15`) will increase as Homebrew updates GCC.
  Always check `ls $(brew --prefix gcc)/bin/g++-*` to confirm the current name.

- If RTLens exits immediately with `Could not find the Qt platform plugin "cocoa"`,
  the venv was created with a non-framework Python. Recreate it with Homebrew Python:

  ```bash
  brew install python@3.12
  /usr/local/opt/python@3.12/bin/python3.12 -m venv .venv --clear
  .venv/bin/pip install -e ".[dev]"
  ```

  On Apple Silicon, replace `/usr/local/opt/` with `/opt/homebrew/opt/`.

- Treat macOS failures as environment constraints for now, not release blockers.

## 5. Optional tools and release links

- `sv2v` (optional recommended): <https://github.com/zachjs/sv2v/releases>
- `surfer` (optional external wave viewer): <https://gitlab.com/surfer-project/surfer/-/releases>
- `netlistsvg` (optional recommended): `npm install -g netlistsvg`

Official Open External wave target scope:

- `surfer`
- `gtkwave`
- `off`

## 6. Validation scope note

- Current release validation focuses on feature availability and functional correctness.
- Large RTL performance has not been fully characterized yet.
- For current testing limitations and planned documentation expansion, see `rtlens/docs/usage.md`.
