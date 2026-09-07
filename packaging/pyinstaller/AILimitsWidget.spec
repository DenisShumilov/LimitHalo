# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


source_root = Path(os.environ["LIMIT_HALO_APP_SOURCE_ROOT"]).resolve(strict=True)
entrypoint = (source_root / os.environ.get("LIMIT_HALO_ENTRYPOINT", "src/limit_halo/__main__.py")).resolve(strict=True)
version_file = Path(os.environ["LIMIT_HALO_VERSION_FILE"]).resolve(strict=True)
manifest_file = Path(os.environ["LIMIT_HALO_WIDGET_MANIFEST"]).resolve(strict=True)
asset_root = (source_root / os.environ.get("LIMIT_HALO_ASSET_ROOT", "src/limit_halo/assets")).resolve()

if source_root not in entrypoint.parents:
    raise ValueError("entrypoint escapes the supplied app source root")

datas = []
for name in ("app.ico", "chatgpt.png", "claude.png", "ATTRIBUTIONS.md", "LICENSE-lobehub.txt", "update_install.ps1"):
    candidate = asset_root / name
    if candidate.is_file() and not candidate.is_symlink():
        datas.append((str(candidate), "limit_halo/assets"))

icon_path = asset_root / "app.ico"
icon = [str(icon_path)] if icon_path.is_file() and not icon_path.is_symlink() else None

a = Analysis(
    [str(entrypoint)],
    pathex=[str(source_root / "src"), str(source_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "webbrowser",
        "requests",
        "httpx",
        "aiohttp",
        "selenium",
        "playwright",
        "pystray",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AILimitsWidget",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(version_file),
    manifest=str(manifest_file),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AILimitsWidget",
)
