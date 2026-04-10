# RTLens

A desktop viewer for SystemVerilog designs — source, hierarchy, signal connectivity, waveform, and schematic.

## Features

- **Source view** — syntax-highlighted SystemVerilog source with jump-to-definition
- **Hierarchy tree** — full instance hierarchy from `slang` IEEE-1800 elaboration
- **Load / driver search** — trace where a signal is driven from or loaded to across hierarchy
- **Waveform viewer** — open `.vcd` / `.fst` wave files; bridge to external viewers (surfer, gtkwave)
- **Schematic view** — RTL block diagram via yosys + netlistsvg (ELK layout)
- **Platform-aware workflow** — Linux primary support, Windows best-effort, macOS reference flow

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | |
| PySide6 ≥ 6.5 | installed via `pip` |
| g++ | for building `slang_dump` helper on first use |
| cmake | for building `slang_dump` helper |
| slang install-prefix | standalone slang v10.0; see [rtlens/docs/install.md](rtlens/docs/install.md) |
| yosys | optional — required for schematic view |
| netlistsvg | optional — required for schematic view |
| node / npm | optional — required for RTL structure view |

## Support Policy

| OS | Support level | Release gate |
|---|---|---|
| Linux | primary | yes |
| Windows | best-effort | no |
| macOS | reference / provisional | no |

## Current Limitations

- Public validation is currently centered on bundled verification RTL (`min_case`, `mid_case`, `deep_case`).
- Large RTL designs are not yet part of the standard regression set.
- Performance characterization (startup / parse / schematic prebuild / memory) is not finalized yet.

## Quick Start

```bash
# 1. Create Python environment
python3 -m venv .venv
. .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

# 2. Install RTLens
pip install -e ".[dev]"

# 3. Build standalone slang (first-time setup)
.venv/bin/python rtlens/tools/setup_slang_prefix.py --clone-if-missing --slang-ref v10.0

# 4. Launch
rtlens --filelist <vlist_file> --top <top_module>
```

See [rtlens/docs/install.md](rtlens/docs/install.md) for Linux/Windows/macOS installation details.

## Usage

```
rtlens [options] [files...]

Options:
  --filelist <file>   file list (one .sv path per line; optional +incdir+, +define+)
  --top <module>      explicit top module name
  --wave <file>       open waveform file (.vcd or .fst) at startup
  --ui qt|tk          GUI backend — qt (default), tk (legacy)
  --editor-cmd <cmd>  external editor command template, e.g. "code --goto {file}:{line}"
```

See [rtlens/docs/usage.md](rtlens/docs/usage.md) for a full feature walkthrough.

Direct runnable samples are documented in `rtlens/docs/usage.md` and can be executed via:

```bash
.venv/bin/python rtlens/tools/run_usage_samples.py --mode run --case mid_case
```

## External Wave Support Scope

Official Open External targets:

- `surfer`
- `gtkwave`
- `off`

## Documentation Roadmap

- Expand Schematic usage documentation with step-by-step navigation and troubleshooting examples.
- Add a dedicated RTL Structure usage section with mode guidance and practical checks.
- Add a performance-measurement guide once larger RTL datasets are available.

## Verification RTL

Sample designs for testing are included under `RTL/verification/`:

| Case | Description |
|---|---|
| `min_case` | minimal single-module design |
| `mid_case` | multi-module design with ALU, lane, router |
| `deep_case` | deeper hierarchy design |

```bash
# Run with the included mid_case sample
rtlens --filelist RTL/verification/mid_case/vlist --top vm_mid_top
```

## AI Assistance

This project was developed with assistance from AI coding tools (Claude Code, Codex).

## License

MIT — see [LICENSE](LICENSE).

Third-party dependency licenses are documented in [DEPENDENCIES.md](DEPENDENCIES.md).
