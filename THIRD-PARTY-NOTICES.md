# Third-party notices

This notice is shipped with binary and source distributions. For a built candidate, the exact dependency inventory is recorded in `release-manifest.json`; every corresponding license, notice, and wheel `METADATA` record is hash-bound by `licenses/dependency-licenses.json` inside both binary distributions.

## Provider names and marks

OpenAI, ChatGPT, Codex, Anthropic, and Claude names and marks belong to their respective owners. Their use identifies compatible services only and does not imply sponsorship, endorsement, or affiliation. LimitHalo is a community project.

Provider marks must not be used as the product icon. The app uses a neutral product identity. If the optional metric-row PNGs are included, they are the unmodified OpenAI and Claude icons from `@lobehub/icons-static-png` version `1.95.0`:

- `dark/openai.png`, shipped as `chatgpt.png`, expected SHA-256 `375016542ebe78b63c7f36b8f6aee3058f2b706d861e5153fd74f9c27f63b8b7`
- `dark/claude.png`, shipped as `claude.png`, expected SHA-256 `4dbf0aa77200058e8cb152e990597a95a99963db85632bf8abd8343a249e898e`

LobeHub distributes that icon library under the MIT License, Copyright (c) 2023 LobeHub. Its full license text must be included as `licenses/LICENSE-lobehub.txt` when those assets ship.

## Exact runtime and packaging inventory

- Python 3.12.13 — Python Software Foundation License; the complete upstream `LICENSE.txt` is shipped as `licenses/PYTHON-LICENSE.txt`.
- Tcl/Tk 8.6 — Tcl/Tk license terms; the collected runtime copy remains under `_internal/_tk_data/license.terms` and a discoverable copy is shipped as `licenses/TCL-TK-LICENSE.terms`.
- altgraph 0.17.5 — exact wheel `altgraph-0.17.5-py2.py3-none-any.whl`.
- packaging 26.3 — exact wheel `packaging-26.3-py3-none-any.whl`.
- pefile 2024.8.26 — exact wheel `pefile-2024.8.26-py3-none-any.whl`.
- Pillow 12.3.0 — exact wheel `pillow-12.3.0-cp312-cp312-win_amd64.whl`.
- PyInstaller 6.21.0 — exact wheel `pyinstaller-6.21.0-py3-none-win_amd64.whl`; GPL-2.0-or-later with the PyInstaller bootloader exception.
- PyInstaller Hooks Contrib 2026.6 — exact wheel `pyinstaller_hooks_contrib-2026.6-py3-none-any.whl`.
- pywin32-ctypes 0.2.3 — exact wheel `pywin32_ctypes-0.2.3-py3-none-any.whl`.
- setuptools 83.0.0 — exact wheel `setuptools-83.0.0-py3-none-any.whl`, including its embedded vendored license, notice, and `METADATA` records.
- `@lobehub/icons-static-png` 1.95.0 — MIT license and asset attribution as described above.

For every locked wheel, the binary payload contains all wheel members named `METADATA` under a `.dist-info` directory and every embedded license, licence, copying, notice, copyright, or author record, including vendored records. They are stored beneath `licenses/python-wheels/` without altering their bytes. The exact wheel filename and SHA-256, component version, copied path, size, role, and SHA-256 are recorded in `licenses/dependency-licenses.json`. This summary does not replace those upstream texts.

## Inno Setup

Inno Setup is a build-time installer compiler. Its license and copyright remain with Jordan Russell, Martijn Laan, and contributors. The pinned compiler is not represented as part of the LimitHalo source license. See the official Inno Setup license accompanying the compiler used for a build.
