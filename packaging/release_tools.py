"""Release package helpers for build_release.ps1 (stage-220).

Run with the build venv python. Subcommands:

  versions  GUI_ROOT                      print the version in pyproject.toml and __init__.py
  stamp     GUI_ROOT VERSION              write VERSION into both files
  precheck  --image I --product-head H
                                          refuse a bad firmware image (before PyInstaller)
  assemble  --gui G --product P --image I --out DIR --version V --gui-commit C
            --product-commit C [--gui-tag T] [--product-tag T]
                                          copy firmware, licences, README, SOURCE, build_info
  zip       DIR ZIP SUMS                  zip DIR (its name is the top folder), write SHA256SUMS
  deps      PRODUCT                       the firmware build dependency checkouts are at BUILD_DEPS

The functions are pure where they can be, so tests/test_release.py checks them
without a build.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
FIRMWARE_DIR = "firmware"            # the folder that update_wizard.bundled_firmware_dir() reads
BUILD_INFO = "build_info.json"       # the file that dialogs.build_info() reads
README_TEMPLATE = HERE / "README.txt"
SOURCE_TEMPLATE = HERE / "SOURCE.txt"
LICENSES = HERE / "licenses"

PRODUCT_URL = "https://github.com/freudenthal/OpenScope-2C53T"
GUI_URL = "https://github.com/freudenthal/OpenScope-2C53T-GUI"

# Build dependencies of the firmware that are not in the product repository
# (.llm specifications.md section 20). Checked against the checkout by
# build_release.ps1 when it can read them.
BUILD_DEPS = {
    "at32_commit": "29f8bfd95b190fa38070d2e0fbbb9f3871b0b3f5",
    "freertos_commit": "83a45bf290024de16ca94df1f7262bcde6c82ffe",
}
TOOLCHAIN = "arm-none-eabi-gcc 13.2.1 20231009 (Ubuntu 15:13.2.rel1-2), GNU make, in WSL 2 Ubuntu"
BUILD_TARGET = "guest-coldtrace"

# (distribution, licence name, files in its dist-info, extra files in packaging/licenses)
THIRD_PARTY = [
    ("PySide6-Essentials", "LGPL-3.0-only (Qt for Python and the Qt libraries in it)",
     "pyside6_essentials-*.dist-info", ["LGPL-3.0.txt"]),
    ("shiboken6", "LGPL-3.0-only", "shiboken6-*.dist-info", ["LGPL-3.0.txt"]),
    ("pyqtgraph", "MIT", "pyqtgraph-*.dist-info", []),
    ("numpy", "BSD-3-Clause (and the licences of the libraries in numpy.libs: OpenBLAS, LAPACK, "
     "GCC runtime)", "numpy-*.dist-info", []),
    ("pyserial", "BSD-3-Clause", "pyserial-*.dist-info", ["pyserial-LICENSE.txt"]),
    ("PyInstaller (bootloader)", "GPL-2.0-or-later with the PyInstaller bootloader exception",
     "pyinstaller-*.dist-info", []),
]

PYPROJECT_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)
INIT_RE = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.M)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ReleaseError(RuntimeError):
    """A reason not to make the package."""


# --- versions --------------------------------------------------------------

def _version_files(gui: Path):
    return [(Path(gui) / "pyproject.toml", PYPROJECT_RE),
            (Path(gui) / "openscope_gui" / "__init__.py", INIT_RE)]


def read_versions(gui: Path) -> List[str]:
    out = []
    for path, rx in _version_files(gui):
        m = rx.search(path.read_text(encoding="utf-8"))
        if not m:
            raise ReleaseError(f"no version in {path}")
        out.append(m.group(2))
    return out


def stamp_version(gui: Path, version: str) -> List[Path]:
    """Write `version` into both files. Returns the files that changed."""
    if not VERSION_RE.match(version):
        raise ReleaseError(f"version {version!r} is not N.N.N")
    changed = []
    for path, rx in _version_files(gui):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        new = rx.sub(lambda m: m.group(1) + version + m.group(3), text, count=1)
        if new != text:
            nl = "\r\n" if b"\r\n" in raw else "\n"
            path.write_bytes(new.replace("\r\n", "\n").replace("\n", nl).encode("utf-8"))
            changed.append(path)
    return changed


# --- firmware --------------------------------------------------------------

def _firmware_lib():
    from openscope import firmware as fw
    return fw


def check_firmware(image: Path, product_head: str):
    """The image's manifest, or ReleaseError with every reason not to ship it."""
    fw = _firmware_lib()
    image = Path(image)
    why = []
    if image.stem.endswith("-dirty"):
        why.append(f"{image.name}: the image was built from a tree with changes (-dirty)")
    mpath = fw.manifest_path_for(image)
    try:
        man = fw.Manifest.load(mpath)
    except fw.ManifestError as e:
        raise ReleaseError(f"{image.name}: {e} (run: openscope manifest {image.name} "
                           f"--target {BUILD_TARGET} --source <product repo> "
                           "--product-commit <full commit>)") from None
    try:
        data = image.read_bytes()
    except OSError as e:
        raise ReleaseError(str(e)) from None
    why += fw.check_image(data, man)
    if man.image and man.image != image.name:
        why.append(f"the manifest names {man.image}, not {image.name}")
    if man.target != BUILD_TARGET:
        why.append(f"target {man.target}, the package ships {BUILD_TARGET}")
    if not man.product_commit:
        why.append("the manifest has no product_commit")
    elif man.product_commit != product_head:
        why.append(f"manifest product_commit {man.product_commit[:7]} is not product HEAD "
                   f"{product_head[:7]}")
    if not man.git_rev or not product_head.startswith(man.git_rev):
        why.append(f"image git revision {man.git_rev} is not product HEAD {product_head[:7]}")
    if why:
        raise ReleaseError("the firmware image cannot go into the package:\n  " + "\n  ".join(why))
    return man


# --- templates -------------------------------------------------------------

FIELD_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def template_fields(text: str) -> set:
    return set(FIELD_RE.findall(text))


def fill(text: str, values: Dict[str, str]) -> str:
    """Replace every {name}. A name with no value, or a value with no name,
    is an error: a release text never ships with a hole in it."""
    names = template_fields(text)
    missing = names - set(values)
    if missing:
        raise ReleaseError("template values missing: " + ", ".join(sorted(missing)))
    extra = set(values) - names
    if extra:
        raise ReleaseError("values not used by the template: " + ", ".join(sorted(extra)))
    return FIELD_RE.sub(lambda m: str(values[m.group(1)]), text)


def _crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


# --- third-party notices ---------------------------------------------------

def _dist_info(site: Path, pattern: str) -> Path:
    found = sorted(Path(site).glob(pattern))
    if len(found) != 1:
        raise ReleaseError(f"{pattern}: {len(found)} folders in {site}, expected 1")
    return found[0]


def _meta(dist: Path, key: str) -> Optional[str]:
    for line in (dist / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(key + ": "):
            return line.split(": ", 1)[1].strip()
        if not line:
            break
    return None


def _licence_files(dist: Path) -> List[Path]:
    files = []
    lic = dist / "licenses"
    if lic.is_dir():
        files += sorted(p for p in lic.rglob("*") if p.is_file())
    for name in ("LICENSE", "LICENSE.txt", "LICENCE", "COPYING", "COPYING.txt"):
        if (dist / name).is_file():
            files.append(dist / name)
    return files


def third_party_notices(site: Path, python_licence: Path, licenses: Path = LICENSES) -> str:
    rule = "=" * 78
    parts = [
        "THIRD-PARTY NOTICES — OpenScope GUI release package for Windows",
        "",
        "This package holds the programs and libraries below in the _internal folder.",
        "Each part keeps its own licence. The licence texts follow, taken from the",
        "installed packages that the build used (their *.dist-info folders).",
        "",
        "OpenScope GUI and the openscope library are GPL-3.0-or-later: see LICENSE.",
        "",
        "Qt for Python (PySide6, shiboken6) and the Qt libraries are used under the",
        "LGPL-3.0. They are separate DLL and .pyd files in _internal\\PySide6 and",
        "_internal\\shiboken6. You can replace them with your own build of the same",
        "version. Source: https://download.qt.io/official_releases/QtForPython/ and",
        "https://code.qt.io/. The GPL-3.0 text that the LGPL-3.0 refers to is LICENSE.",
        "",
        "The Microsoft Visual C++ runtime DLLs (vcruntime140*.dll, msvcp140*.dll) come",
        "with Python and numpy under the Microsoft Visual C++ redistributable terms.",
        "",
    ]
    py_ver = ".".join(str(x) for x in sys.version_info[:3])
    body = [(f"Python {py_ver}", "PSF-2.0 (Python Software Foundation License)",
             [Path(python_licence)])]
    for name, licence, pattern, extra in THIRD_PARTY:
        dist = _dist_info(site, pattern)
        ver = _meta(dist, "Version") or "?"
        files = [p for p in _licence_files(dist)
                 if "Qt-Commercial" not in p.name]        # the commercial option is not used
        files += [Path(licenses) / e for e in extra]
        if not files:
            raise ReleaseError(f"{name}: no licence text found")
        body.append((f"{name} {ver}", licence, files))
    for title, licence, files in body:
        parts += [rule, f"{title}", f"Licence: {licence}", rule, ""]
        for f in files:
            parts += [f"--- {f.name} ---", f.read_text(encoding="utf-8", errors="replace").rstrip(), ""]
    return "\n".join(parts) + "\n"


# --- assemble --------------------------------------------------------------

def source_values(man, gui_commit: str, product_commit: str,
                  gui_tag: str = "", product_tag: str = "", version: str = "") -> Dict[str, str]:
    return {
        "version": version,
        "product_url": PRODUCT_URL,
        "product_commit": product_commit,
        "product_tag": product_tag or "(no tag)",
        "gui_url": GUI_URL,
        "gui_commit": gui_commit,
        "gui_tag": gui_tag or "(no tag)",
        "image_name": man.image,
        "image_sha256": man.sha256,
        "image_size": str(man.size),
        "build_time": man.build_time or "?",
        "toolchain": TOOLCHAIN,
        "build_target": BUILD_TARGET,
        **BUILD_DEPS,
    }


def readme_values(man, version: str, gui_commit: str) -> Dict[str, str]:
    return {
        "version": version,
        "gui_commit": gui_commit[:7],
        "image_name": man.image,
        "firmware_rev": man.git_rev or "?",
        "build_time": man.build_time or "?",
        "api_level": str(man.api_level),
    }


def assemble(gui: Path, product: Path, image: Path, out: Path, version: str,
             gui_commit: str, product_commit: str, gui_tag: str = "", product_tag: str = "",
             site: Optional[Path] = None, python_licence: Optional[Path] = None) -> dict:
    out = Path(out)
    if not (out / "openscope-gui.exe").is_file():
        raise ReleaseError(f"{out} has no openscope-gui.exe (PyInstaller output)")
    man = check_firmware(image, product_commit)
    fwdir = out / FIRMWARE_DIR
    fwdir.mkdir()
    shutil.copy2(image, fwdir / image.name)
    fw = _firmware_lib()
    shutil.copy2(fw.manifest_path_for(image), fwdir / fw.manifest_path_for(image).name)
    (fwdir / "SOURCE.txt").write_bytes(_crlf(fill(
        SOURCE_TEMPLATE.read_text(encoding="utf-8"),
        source_values(man, gui_commit, product_commit, gui_tag, product_tag, version))))
    (out / "README.txt").write_bytes(_crlf(fill(
        README_TEMPLATE.read_text(encoding="utf-8"), readme_values(man, version, gui_commit))))
    shutil.copy2(Path(gui) / "LICENSE", out / "LICENSE.txt")
    site = Path(site) if site else Path(sys.prefix) / "Lib" / "site-packages"
    python_licence = Path(python_licence) if python_licence else Path(sys.base_prefix) / "LICENSE.txt"
    (out / "THIRD-PARTY-NOTICES.txt").write_bytes(_crlf(third_party_notices(site, python_licence)))
    from openscope import __version__ as lib_version
    info = {
        "version": version,
        "gui_commit": gui_commit,
        "gui_tag": gui_tag or None,
        "library_version": lib_version,
        "library_commit": product_commit,
        "product_tag": product_tag or None,
        "firmware": json.loads(man.to_json()),
        "built": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": ".".join(str(x) for x in sys.version_info[:3]),
    }
    try:
        import PyInstaller
        info["pyinstaller"] = PyInstaller.__version__
    except ImportError:                     # pragma: no cover
        pass
    (out / BUILD_INFO).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def make_zip(folder: Path, zip_path: Path, sums: Path) -> None:
    folder, zip_path = Path(folder), Path(zip_path)
    if zip_path.exists():
        raise ReleaseError(f"{zip_path} exists; it is never overwritten")
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, Path(folder.name) / p.relative_to(folder))
    lines = [f"{sha256(zip_path)}  {zip_path.name}"]
    for img in sorted((folder / FIRMWARE_DIR).glob("*.bin")):
        lines.append(f"{sha256(img)}  {folder.name}/{FIRMWARE_DIR}/{img.name}")
    Path(sums).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def check_deps(product: Path) -> List[str]:
    """Reasons the build dependency checkouts are not the commits SOURCE.txt
    names. [] = they are."""
    import subprocess
    why = []
    for key, sub in (("at32_commit", "at32f403a_lib"), ("freertos_commit", "FreeRTOS")):
        d = Path(product) / "firmware" / sub
        try:
            head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            why.append(f"cannot read the commit of {d}")
            continue
        if head != BUILD_DEPS[key]:
            why.append(f"{d} is at {head[:7]}, SOURCE.txt names {BUILD_DEPS[key][:7]}")
    return why


# --- CLI -------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="release_tools.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("versions"); s.add_argument("gui")
    s = sub.add_parser("stamp"); s.add_argument("gui"); s.add_argument("version")
    s = sub.add_parser("precheck")
    s.add_argument("--image", required=True); s.add_argument("--product-head", required=True)
    s = sub.add_parser("assemble")
    for k in ("gui", "product", "image", "out", "version", "gui-commit", "product-commit"):
        s.add_argument("--" + k, required=True)
    s.add_argument("--gui-tag", default=""); s.add_argument("--product-tag", default="")
    s = sub.add_parser("zip"); s.add_argument("folder"); s.add_argument("zip"); s.add_argument("sums")
    s = sub.add_parser("deps"); s.add_argument("product")
    a = p.parse_args(argv)
    try:
        if a.cmd == "deps":
            why = check_deps(Path(a.product))
            if why:
                raise ReleaseError("build dependencies:\n  " + "\n  ".join(why))
            print("deps      at32f403a_lib and FreeRTOS at the commits in SOURCE.txt")
        elif a.cmd == "versions":
            print(" ".join(read_versions(Path(a.gui))))
        elif a.cmd == "stamp":
            for f in stamp_version(Path(a.gui), a.version):
                print(f"stamped {f}")
        elif a.cmd == "precheck":
            m = check_firmware(Path(a.image), a.product_head)
            print(f"firmware  {m.image}  {m.git_rev}  {m.build_time}  API {m.api_level}  {m.sha256}")
        elif a.cmd == "assemble":
            info = assemble(Path(a.gui), Path(a.product), Path(a.image), Path(a.out), a.version,
                            a.gui_commit, a.product_commit, a.gui_tag, a.product_tag)
            print(f"assembled {a.out}  library {info['library_version']}")
        elif a.cmd == "zip":
            make_zip(Path(a.folder), Path(a.zip), Path(a.sums))
            print(f"zip       {a.zip}")
    except ReleaseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
