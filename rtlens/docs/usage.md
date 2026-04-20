# RTLens Usage Guide

## Launch

```bash
# Basic launch
rtlens --filelist path/to/vlist --top MyTopModule

# With waveform at startup
rtlens --filelist path/to/vlist --top MyTopModule --wave path/to/dump.vcd

# With external editor template
rtlens --filelist path/to/vlist --top MyTopModule --editor-cmd "code --goto {file}:{line}"
```

運用引き継ぎ（Codex向け）は [HANDOVER_CODEX_JA.md](HANDOVER_CODEX_JA.md) を参照してください。

## Runnable verification samples (repository included RTL)

Linux/macOS:

```bash
.venv/bin/python -m rtlens --ui qt --filelist RTL/verification/min_case/vlist --top vm_min_top
.venv/bin/python -m rtlens --ui qt --filelist RTL/verification/mid_case/vlist --top vm_mid_top
.venv/bin/python -m rtlens --ui qt --filelist RTL/verification/deep_case/vlist --top vm_deep_top
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m rtlens --ui qt --filelist RTL/verification/min_case/vlist --top vm_min_top
.\.venv\Scripts\python.exe -m rtlens --ui qt --filelist RTL/verification/mid_case/vlist --top vm_mid_top
.\.venv\Scripts\python.exe -m rtlens --ui qt --filelist RTL/verification/deep_case/vlist --top vm_deep_top
```

With bundled sample VCD:

```bash
.venv/bin/python -m rtlens --ui qt --filelist RTL/verification/min_case/vlist --top vm_min_top --wave RTL/verification/min_case/wave/vm_min_top_sample.vcd
```

## Usage sample helper script

Use the helper to print or run canonical sample commands:

```bash
.venv/bin/python rtlens/tools/run_usage_samples.py --mode print --case all
.venv/bin/python rtlens/tools/run_usage_samples.py --mode run --case mid_case
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe rtlens/tools/run_usage_samples.py --mode print --case all
.\.venv\Scripts\python.exe rtlens/tools/run_usage_samples.py --mode run --case mid_case
```

## Filelist format

`--filelist` accepts one entry per line:

```text
# comments are ignored
+incdir+path/to/include
+define+MY_DEFINE=1
path/to/file_a.sv
path/to/file_b.sv
```

## GUI overview

### Hierarchy tab

- Displays full instance hierarchy from slang elaboration.
- Click instance to move source focus.
- Double click instance to navigate into child context.

### Source tab

- Shows SystemVerilog source.
- Signal clicks can drive Load/Driver searches.

### Signal search (Load/Driver)

1. Select a signal from Source or type signal name.
2. Choose `Drivers` or `Loads`.
3. Optionally enable `Include port sites`.

### Schematic tab

Requires `yosys` and `netlistsvg`.

- Renders selected module block diagram.
- Click/Double click enables instance navigation.
- Open External supports zoom/Fit and `Ctrl + double click` transitions.

### RTL Structure tab

Requires `node`, `npm`, and Graphviz (`dot`) in the current setup.

- Use this tab for high-level hierarchy/structure inspection.
- Rendering behavior depends on available layout backend and host toolchain.
- For quick checks, compare visual topology against the same design in Schematic.

### Waveform tab

- Opens `.vcd` and `.fst` (when conversion tools are available).
- Open External wave scope is: `surfer`, `gtkwave`, `off`.

## Qt shortcuts

The Qt UI supports configurable reload shortcuts.

Default shortcuts:

- `Reload RTL`: `Ctrl+R`
- `Reload All`: `Ctrl+Shift+R`
- `Reload Wave`: `Ctrl+Shift+W`

Shortcut config file:

- Default path: `~/.config/rtlens/shortcuts_qt.json`
- If `XDG_CONFIG_HOME` is set, the path is `${XDG_CONFIG_HOME}/rtlens/shortcuts_qt.json`

Example:

```json
{
  "reload_rtl": ["Ctrl+R"],
  "reload_all": ["Ctrl+Shift+R", "F5"],
  "reload_wave": ["Ctrl+Shift+W"]
}
```

Notes:

- You can specify one string or a list of strings for each action.
- Set an action to `[]` to disable its shortcut.
- Invalid key strings are ignored and reported in the status area.
- Shortcut names follow `QKeySequence` notation (for example `Ctrl+R`, `F5`).
- Restart RTLens after editing `shortcuts_qt.json`.

## Known limitations

- Verified regression inputs are primarily the bundled `RTL/verification/*` cases.
- Large RTL projects are not yet covered by a standardized public regression flow.
- Performance metrics for large designs are not finalized in this release cycle.

## Planned documentation expansion

- Add detailed Schematic operation guides (selection, navigation, Open External flow, Zoom/Fit tips).
- Expand RTL Structure documentation with mode selection, expected outputs, and failure triage.
- Add a benchmark and performance section after larger RTL test sets become available.

## Environment variables

| Variable | Description |
|---|---|
| `RTLENS_SLANG_ROOT` | standalone slang install-prefix path (default: `.deps/slang`) |
| `RTLENS_CMAKE_GENERATOR` | override CMake generator for `slang_dump` build |
| `RTLENS_CXX_COMPILER` | override C++ compiler path |
| `RTLENS_WINDOWS_SLANG_RETRY` | set `1` to bypass per-session crash guard on Windows |

## Verification

```bash
# Linux strict baseline check
.venv/bin/python rtlens/tools/verify_install.py --target-os linux --strict

# Test suite
.venv/bin/python -m pytest -q rtlens/tests
```
