"""Release package checks (stage-220). Pure: no build, no PyInstaller.

The spec, the build script and the wizard agree on the `firmware` folder; the
README and SOURCE templates hold the required items and fill with no hole;
the version is the same in pyproject.toml and __init__.py; the firmware gate
refuses a -dirty name, a wrong product commit and a changed image; About shows
the release's commits and bundled firmware.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401

ROOT = Path(_paths.ROOT)
PKG = ROOT / "packaging"

_spec = importlib.util.spec_from_file_location("release_tools", PKG / "release_tools.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


class TestLayout(unittest.TestCase):
    def test_spec_names_the_firmware_folder(self):
        spec = (PKG / "openscope-gui.spec").read_text(encoding="utf-8")
        self.assertRegex(spec, r'FIRMWARE_DIR\s*=\s*"firmware"')
        self.assertEqual(rt.FIRMWARE_DIR, "firmware")
        self.assertRegex(spec, r"console\s*=\s*False")
        self.assertEqual(len(re.findall(r"upx\s*=\s*False", spec)), 2)
        self.assertIn('name=NAME', spec)
        self.assertRegex(spec, r'NAME\s*=\s*"openscope-gui"')

    def test_wizard_reads_the_folder_next_to_the_exe(self):
        try:
            from openscope_gui import update_wizard
        except ImportError as e:                       # pragma: no cover
            self.skipTest(f"PySide6 missing: {e}")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / rt.FIRMWARE_DIR).mkdir()
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(Path(d) / "openscope-gui.exe")):
                self.assertEqual(update_wizard.bundled_firmware_dir(), Path(d).resolve() / "firmware")

    def test_build_script_steps(self):
        ps = (PKG / "build_release.ps1").read_text(encoding="utf-8")
        for item in ("status", "--porcelain", "precheck", "deps", "PyInstaller", "openscope-gui.spec",
                     "assemble", "SHA256SUMS.txt", '"0.2.0"', "releases", "--no-index"):
            self.assertIn(item, ps)
        self.assertTrue(all(ord(c) < 128 for c in ps), "PowerShell 5.1 reads a file with no BOM as ANSI")

    def test_versions_equal(self):
        a, b = rt.read_versions(ROOT)
        self.assertEqual(a, b)
        from openscope_gui import __version__
        self.assertEqual(__version__, a)

    def test_stamp_version(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "openscope_gui").mkdir()
            (d / "pyproject.toml").write_bytes(b'[project]\r\nname = "x"\r\nversion = "0.1.0"\r\n')
            (d / "openscope_gui" / "__init__.py").write_text('"""doc"""\n\n__version__ = "0.1.0"\n')
            self.assertEqual(len(rt.stamp_version(d, "0.3.1")), 2)
            self.assertEqual(rt.read_versions(d), ["0.3.1", "0.3.1"])
            self.assertIn(b'version = "0.3.1"\r\n', (d / "pyproject.toml").read_bytes())
            self.assertEqual(rt.stamp_version(d, "0.3.1"), [])
            with self.assertRaises(rt.ReleaseError):
                rt.stamp_version(d, "0.3")


class TestTemplates(unittest.TestCase):
    README_ITEMS = [
        "Unzip", "usbser.inf", "Back up calibration", "Copy that folder to a second place",
        r"%LOCALAPPDATA%\OpenScope\backups", "Update firmware", "MENU", "reset button",
        "IAP", "openscope update", "FNIRSI file", "SmartScreen", "Run anyway",
        "Volts are not calibrated across units", "DC volts only", "seam", "STOP",
        "Math", "THIRD-PARTY-NOTICES.txt", "SOURCE.txt", "FPGA", "SHA256SUMS.txt",
    ]
    SOURCE_ITEMS = [
        "{product_commit}", "{product_url}", "{gui_commit}", "{gui_url}", "{at32_commit}",
        "{freertos_commit}", "{toolchain}", "make {build_target}", "Written offer",
        "section 6", "three years", "FPGA", "115,638", "not GPL", "{image_sha256}",
    ]

    def test_readme_items(self):
        text = (PKG / "README.txt").read_text(encoding="utf-8")
        for item in self.README_ITEMS:
            self.assertIn(item, text)

    def test_source_items(self):
        text = (PKG / "SOURCE.txt").read_text(encoding="utf-8")
        for item in self.SOURCE_ITEMS:
            self.assertIn(item, text)

    def _manifest(self):
        return mock.Mock(image="guest-coldtrace-abc1234.bin", sha256="0" * 64, size=1234,
                         build_time="Nov 25 2026 09:10:30", git_rev="abc1234", api_level=1)

    def test_templates_fill_with_no_hole(self):
        m = self._manifest()
        src = rt.fill((PKG / "SOURCE.txt").read_text(encoding="utf-8"),
                      rt.source_values(m, "g" * 40, "p" * 40, "v0.2.0", "usb-link-gui-v0.2.0", "0.2.0"))
        self.assertNotRegex(src, r"\{[a-z][a-z0-9_]*\}")
        for v in ("g" * 40, "p" * 40, rt.BUILD_DEPS["at32_commit"], rt.BUILD_DEPS["freertos_commit"],
                  "usb-link-gui-v0.2.0", "make guest-coldtrace"):
            self.assertIn(v, src)
        rd = rt.fill((PKG / "README.txt").read_text(encoding="utf-8"),
                     rt.readme_values(m, "0.2.0", "g" * 40))
        self.assertNotRegex(rd, r"\{[a-z][a-z0-9_]*\}")
        self.assertIn("guest-coldtrace-abc1234.bin", rd)

    def test_fill_refuses_holes_and_extras(self):
        with self.assertRaises(rt.ReleaseError):
            rt.fill("a {x} {y}", {"x": "1"})
        with self.assertRaises(rt.ReleaseError):
            rt.fill("a {x}", {"x": "1", "z": "2"})


class TestNotices(unittest.TestCase):
    def test_notices_name_every_part(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d)
            for pattern, *_ in [(p[2],) for p in rt.THIRD_PARTY]:
                dist = site / pattern.replace("*", "9.9")
                (dist / "licenses").mkdir(parents=True)
                (dist / "METADATA").write_text("Name: x\nVersion: 9.9\n\nbody\n")
                (dist / "licenses" / "LICENSE.txt").write_text(f"text of {pattern}\n")
                (dist / "licenses" / "LicenseRef-Qt-Commercial.txt").write_text("COMMERCIAL\n")
            py = site / "PYLICENSE.txt"
            py.write_text("PSF text\n")
            text = rt.third_party_notices(site, py)
        for item in ("PySide6-Essentials 9.9", "LGPL-3.0", "GNU LESSER GENERAL PUBLIC LICENSE",
                     "pyqtgraph 9.9", "MIT", "numpy 9.9", "BSD-3-Clause", "pyserial 9.9",
                     "Chris Liechti", "PSF", "PSF text", "PyInstaller", "replace them"):
            self.assertIn(item, text)
        self.assertNotIn("COMMERCIAL", text)

    def test_missing_dist_info_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(rt.ReleaseError):
                rt.third_party_notices(Path(d), Path(d) / "x")


class TestFirmwareGate(unittest.TestCase):
    def _image(self, d, name="guest-coldtrace-abc1234.bin"):
        from openscope import firmware as fw
        data = bytearray(4096)
        data[0:4] = (0x20037FE0).to_bytes(4, "little")
        data[4:8] = (0x08007101).to_bytes(4, "little")
        text = b"OpenScope Nov 25 2026 09:10:30 abc1234\x00guest-coldtrace\x00"
        data[256:256 + len(text)] = text
        img = Path(d) / name
        img.write_bytes(bytes(data))
        man = fw.Manifest(target="guest-coldtrace", version="t", git_rev="abc1234",
                          build_time="Nov 25 2026 09:10:30", api_level=1, size=len(data),
                          sha256=__import__("hashlib").sha256(bytes(data)).hexdigest(),
                          product_commit="abc1234" + "0" * 33, image=name)
        man.save(fw.manifest_path_for(img))
        return img, man

    def test_gate(self):
        with tempfile.TemporaryDirectory() as d:
            img, man = self._image(d)
            with mock.patch.object(rt._firmware_lib(), "check_image", return_value=[]):
                self.assertEqual(rt.check_firmware(img, man.product_commit).sha256, man.sha256)
                with self.assertRaisesRegex(rt.ReleaseError, "product HEAD"):
                    rt.check_firmware(img, "def5678" + "0" * 33)
                # same short revision, other full commit: the manifest commit decides
                with self.assertRaisesRegex(rt.ReleaseError, "manifest product_commit"):
                    rt.check_firmware(img, "abc1234" + "1" * 33)
            with mock.patch.object(rt._firmware_lib(), "check_image", return_value=["SHA-256 differs"]):
                with self.assertRaisesRegex(rt.ReleaseError, "SHA-256 differs"):
                    rt.check_firmware(img, man.product_commit)
            dirty, m2 = self._image(d, "guest-coldtrace-abc1234-dirty.bin")
            with mock.patch.object(rt._firmware_lib(), "check_image", return_value=[]):
                with self.assertRaisesRegex(rt.ReleaseError, "-dirty"):
                    rt.check_firmware(dirty, m2.product_commit)

    def test_real_check_image_refuses_a_changed_image(self):
        with tempfile.TemporaryDirectory() as d:
            img, man = self._image(d)
            img.write_bytes(img.read_bytes()[:-1] + b"\x01")
            with self.assertRaises(rt.ReleaseError):
                rt.check_firmware(img, man.product_commit)

    def test_no_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "guest-coldtrace-abc1234.bin"
            img.write_bytes(b"x")
            with self.assertRaisesRegex(rt.ReleaseError, "openscope manifest"):
                rt.check_firmware(img, "abc1234")


class TestAbout(unittest.TestCase):
    def setUp(self):
        try:
            from openscope_gui import dialogs
        except ImportError as e:                       # pragma: no cover
            self.skipTest(f"PySide6 missing: {e}")
        self.dialogs = dialogs

    INFO = {"version": "0.2.0", "gui_commit": "1234567890abcdef", "library_commit": "fedcba0987654321",
            "firmware": {"target": "guest-coldtrace", "git_rev": "979f127",
                         "build_time": "Nov 25 2026 09:10:30", "api_level": 1}}

    def test_about_with_build_info(self):
        t = self.dialogs.about_text("OpenScope x", self.INFO)
        for item in ("1234567", "fedcba0", "guest-coldtrace 979f127", "Nov 25 2026 09:10:30",
                     "API level 1", "OpenScope x"):
            self.assertIn(item, t)
        from openscope import __version__ as lib
        from openscope_gui import __version__ as gui
        self.assertIn(gui, t)
        self.assertIn(lib, t)

    def test_about_from_source(self):
        t = self.dialogs.about_text("", {})
        self.assertIn("not a release package", t)
        self.assertIn("not connected", t)

    def test_build_info_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self.dialogs.build_info(Path(d)))
            (Path(d) / "build_info.json").write_text(json.dumps(self.INFO))
            self.assertEqual(self.dialogs.build_info(Path(d))["gui_commit"], "1234567890abcdef")
            (Path(d) / "build_info.json").write_text("not json")
            self.assertIsNone(self.dialogs.build_info(Path(d)))
        self.assertIsNone(self.dialogs.build_info())    # not frozen


if __name__ == "__main__":
    unittest.main()
