# macOS PySide6 QPA Plugin Debug Notes

This note tracks a macOS-specific failure seen while validating the Qt GUI flow.

## Symptom

The GUI regression helper can fail before the RTLens window is created:

```bash
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

Observed error:

```text
qt.qpa.plugin: Could not find the Qt platform plugin "cocoa" in ""
This application failed to start because no Qt platform plugin could be initialized.
```

This is different from a missing `PySide6` Python package. In the observed case,
`import PySide6` worked and the `libqcocoa.dylib` plugin existed under the venv.

## What To Check

Run the macOS Qt plugin diagnostic:

```bash
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --check
```

Manual checks:

```bash
.venv/bin/python -c "import PySide6; from pathlib import Path; p=Path(PySide6.__file__).resolve().parent; print(p); print(sorted(x.name for x in (p/'Qt'/'plugins'/'platforms').glob('*')))"
ls -lO@ .venv/lib/python*/site-packages/PySide6/Qt/plugins/platforms
```

If `ls -lO@` shows `hidden` on `libqcocoa.dylib`, Qt may not load the platform
plugin even though the file exists and `QLibraryInfo` points to the right plugin
directory.

Minimal PySide6 reproduction:

```bash
cd rtlens
../.venv/bin/python -c "from PySide6.QtWidgets import QApplication; app=QApplication([]); print(app.platformName())"
```

If this fails with the same `cocoa` plugin message, the failure is below RTLens
application code and should be debugged as a PySide6/Qt runtime issue first.

## Temporary Recovery

Keep the standard `.venv` layout first. Clear the macOS hidden flag from the venv:

```bash
.venv/bin/python rtlens/tools/macos_qt_plugin_fix.py --fix-hidden-flags --venv .venv
```

Equivalent shell command:

```bash
chflags -R nohidden .venv
```

Then rerun the minimal PySide6 reproduction and the GUI smoke:

```bash
cd rtlens
../.venv/bin/python -c "from PySide6.QtWidgets import QApplication; app=QApplication([]); print(app.platformName())"
cd ..
.venv/bin/python rtlens/tools/gui_regression_cases.py --mode run --case mid_case
```

If the hidden flag returns or the plugin still cannot be loaded, create the venv
in a non-hidden directory such as `venv` as a fallback. The preferred setup remains
`.venv` unless this macOS file-flag issue is observed.

## Notes

Setting `QT_QPA_PLATFORM_PLUGIN_PATH`, `QT_PLUGIN_PATH`, or `QT_QPA_PLATFORM`
does not necessarily recover this failure. In the observed failure, Qt already
checked the correct PySide6 plugin directory but did not accept the platform
plugin files as loadable candidates.
