# -*- mode: python ; coding: utf-8 -*-
#
# Builds a single-file AgenticIAM executable. Run from the repo root with:
#   pyinstaller agenticiam.spec
#
# `cryptography` is excluded: nothing in this project imports it, but on
# some Linux distros a broken/incompatible system-wide install gets pulled
# into PyInstaller's module scan (a Rust/pyo3 init panic) purely because
# it's importable, so it's excluded defensively.

a = Analysis(
    ['scripts/entrypoint.py'],
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
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
