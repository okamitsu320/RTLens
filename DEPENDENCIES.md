# Third-Party Dependencies and License Notes

This document tracks third-party dependencies used by `RTLens` for release preparation.

## 1. Project License

| Item | Value |
|---|---|
| Project | RTLens |
| License SPDX | MIT |
| Source | `LICENSE` |

## 2. Python Runtime Dependencies

Distribution type: PyPI/runtime dependency (not vendored in this repository).

| Name | Version source | Role | SPDX (declared) | Verification source |
|---|---|---|---|---|
| PySide6 | `pyproject.toml` + installed metadata | Qt GUI runtime | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` | `importlib.metadata("PySide6")` |
| shiboken6 | installed metadata (transitive) | PySide6 runtime dependency | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` | `importlib.metadata("shiboken6")` |
| PySide6_Essentials | installed metadata (transitive) | PySide6 runtime dependency | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` | `importlib.metadata("PySide6_Essentials")` |
| PySide6_Addons | installed metadata (transitive) | PySide6 runtime dependency | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` | `importlib.metadata("PySide6_Addons")` |

Notes:
- Binary redistribution obligations must be reviewed separately from source-only publication.

## 3. Vendored Node Dependencies in Repository

Distribution type: repository-tracked files under `third_party/elk`.

| Name | Version source | Role | SPDX (declared) | Verification source |
|---|---|---|---|---|
| elk (wrapper package) | `third_party/elk/package.json` | local ELK runner package | `ISC` | `third_party/elk/package.json` / lock |
| elkjs | `third_party/elk/package-lock.json` | RTL Structure ELK engine | `EPL-2.0` | `third_party/elk/package-lock.json` |

## 4. External Tool Dependencies (User-Installed, Not Vendored)

Distribution type: user/toolchain installation outside this repository.

| Tool | Role | SPDX | Verification source | Status |
|---|---|---|---|---|
| g++ (GCC) | build `slang_dump` helper | `GPL-3.0-or-later` | https://gcc.gnu.org/onlinedocs/gcc/Copying.html | required |
| yosys | schematic backend | `ISC` | https://github.com/YosysHQ/yosys/blob/main/COPYING | required |
| node | runtime for ELK runner | `MIT` | https://github.com/nodejs/node/blob/main/LICENSE | required |
| npm | installs ELK deps | `Artistic-2.0` | https://github.com/npm/cli/blob/latest/LICENSE | required |
| graphviz (`dot`) | RTL Structure Graphviz renderer | `EPL-1.0` | https://gitlab.com/graphviz/graphviz/-/blob/main/LICENSE | optional (required for RTL Structure) |
| netlistsvg | schematic renderer | `ISC` | https://github.com/nturley/netlistsvg/blob/master/LICENSE | optional recommended |
| sv2v | SV-to-Verilog conversion | `MIT` | https://github.com/zachjs/sv2v/blob/master/LICENSE | optional recommended |
| fst2vcd | `.fst` to `.vcd` conversion path | `GPL-2.0-only` | https://github.com/gtkwave/gtkwave/blob/master/COPYING | optional (required for `.fst` import) |
| gtkwave | FST conversion / external wave integration | `GPL-2.0-only` | https://github.com/gtkwave/gtkwave/blob/master/COPYING | optional |
| surfer | external wave viewer bridge | `EUPL-1.2` | https://gitlab.com/surfer-project/surfer/-/blob/main/LICENSE | optional |

## 5. OS Runtime Integrations (Not Redistributed)

Distribution type: OS-provided opener/runtime integration.

| Tool/API | Role | SPDX | Verification source | Status |
|---|---|---|---|---|
| `xdg-open` (Linux) | Open External fallback opener | `NOASSERTION` | distro package metadata (`xdg-utils`) | required for external-open fallback on Linux |
| `open` (macOS) | Open External fallback opener | `NOASSERTION` | Apple platform tool metadata | required for external-open fallback on macOS |
| `os.startfile` (Windows API) | Open External fallback opener | `NOASSERTION` | Windows platform API | required for external-open fallback on Windows |
| `tkinter` (`python3-tk`) | Tk UI backend runtime (`--ui tk`) | `NOASSERTION` | Python distro / OS package metadata | optional (required for Tk UI) |

## 6. Slang Link Dependencies (Standalone Only)

`RTLens` builds `slang_dump` from a standalone slang install-prefix.

| Artifact family | Role | SPDX | Verification source |
|---|---|---|---|
| slang library | `slang_dump` link dependency | `MIT` | https://github.com/MikePopoloski/slang/blob/master/LICENSE |
| fmt library | transitive slang dependency | `MIT` | https://github.com/fmtlib/fmt/blob/master/LICENSE |
| mimalloc library | transitive slang dependency (when enabled) | `MIT` | https://github.com/microsoft/mimalloc/blob/master/LICENSE |

## 7. Action Items

1. Replace `NOASSERTION` entries with concrete SPDX identifiers before public binary distribution.
2. Keep this file synchronized when adding new dependencies (Python, Node, or external tools).
3. If distribution package includes third-party binaries, perform dedicated legal review and add NOTICE/license bundle policy.
