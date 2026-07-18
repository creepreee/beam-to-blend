"""Assemble an installable Blender add-on zip.

Produces ``dist/beamng_cache_importer.zip`` containing a single top-level
package ``beamng_cache_importer/`` with:

    beamng_cache_importer/
        __init__.py        (from addon/)
        ui.py, operators.py, preferences.py
        importer/          (core library, bundled)
        runtime/           (Blender runtime, bundled)

Install in Blender via Edit > Preferences > Add-ons > Install..., pick the zip.
The add-on's __init__ puts its own dir on sys.path so the bundled `importer`
and `runtime` packages resolve as top-level imports.

Run:  python build_addon.py
"""

import os
import shutil
import zipfile

_REPO = os.path.dirname(os.path.abspath(__file__))
_DIST = os.path.join(_REPO, "dist")
_PKG_NAME = "beamng_cache_importer"
_STAGE = os.path.join(_DIST, _PKG_NAME)


def _copy_py_tree(src_dir: str, dst_dir: str) -> None:
    """Copy a package's .py files (skip __pycache__ and non-source)."""
    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, src_dir)
        target = os.path.join(dst_dir, rel) if rel != "." else dst_dir
        os.makedirs(target, exist_ok=True)
        for f in files:
            if f.endswith(".py"):
                shutil.copy2(os.path.join(root, f), os.path.join(target, f))


def build() -> str:
    if os.path.exists(_STAGE):
        shutil.rmtree(_STAGE)
    os.makedirs(_STAGE, exist_ok=True)

    # Add-on modules go at the package root.
    for name in ("__init__.py", "ui.py", "operators.py", "preferences.py"):
        shutil.copy2(os.path.join(_REPO, "addon", name), os.path.join(_STAGE, name))

    # Bundle the core packages inside the add-on package.
    _copy_py_tree(os.path.join(_REPO, "importer"), os.path.join(_STAGE, "importer"))
    _copy_py_tree(os.path.join(_REPO, "runtime"), os.path.join(_STAGE, "runtime"))

    zip_path = os.path.join(_DIST, f"{_PKG_NAME}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(_STAGE):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, _DIST)  # keep beamng_cache_importer/ prefix
                zf.write(full, arc)

    return zip_path


if __name__ == "__main__":
    path = build()
    print(f"Built add-on: {path}")
