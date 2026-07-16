# -*- mode: python ; coding: utf-8 -*-
#
# Builds two single-file AgenticIAM executables. Run from the repo root with:
#   pyinstaller agenticiam.spec
#
# - agenticiam(.exe): the CLI (console app) — init/agent/role/serve/mcp/...
# - agenticiam-gui(.exe): windowed (no console) — starts the server and
#   opens the web admin console in the default browser, for double-click use.
#
# `cryptography` is excluded: nothing in this project imports it, but on
# some Linux distros a broken/incompatible system-wide install gets pulled
# into PyInstaller's module scan (a Rust/pyo3 init panic) purely because
# it's importable, so it's excluded defensively.

common_kwargs = dict(
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cryptography'],
    noarchive=False,
    optimize=0,
)

cli_analysis = Analysis(['scripts/entrypoint.py'], **common_kwargs)
cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    cli_analysis.binaries,
    cli_analysis.datas,
    [],
    name='agenticiam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

gui_analysis = Analysis(['scripts/gui_entrypoint.py'], **common_kwargs)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    gui_analysis.binaries,
    gui_analysis.datas,
    [],
    name='agenticiam-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
