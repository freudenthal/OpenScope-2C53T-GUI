"""Stage-210: firmware compatibility, the calibration backup job and the
update wizard, offscreen, with a fake device, a fake drive probe and a fake
library flow (no port, no drive). One test runs the real library flow
(update.run_update) against the library's own fake unit, when the library
tests are next to the library.

Only a missing PySide6/numpy/pyqtgraph skips (stage-180 rule)."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)
from openscope import firmware as fw  # noqa: E402
from openscope import proto  # noqa: E402
from openscope import update as up  # noqa: E402
from openscope.device import Cancelled, Timeout  # noqa: E402
from openscope.link import NoDevice  # noqa: E402

HAVE_QT = all(importlib.util.find_spec(m) is not None for m in ("PySide6", "numpy", "pyqtgraph"))
if HAVE_QT:
    from PySide6 import QtCore, QtWidgets
    from openscope_gui import labels
    from openscope_gui import update_wizard as uw
    from openscope_gui.app import MainWindow
    from openscope_gui.labels import Compat
    from openscope_gui.worker import Worker
    from test_gui_worker import FakeDevice, Opener

UID = bytes(range(12))
IDENT = proto.Identity(UID, 1, "guest-coldtrace", "cb8a172")
FW = "OpenScope Nov 25 2026 23:12:35 cb8a172"


def manifest(build="Nov 26 2026 10:00:00", api=1, rev="abc1234", image=None) -> "fw.Manifest":
    return fw.Manifest(target="guest-coldtrace", version=f"guest-coldtrace {rev}", git_rev=rev,
                       build_time=build, api_level=api, size=16, sha256="0" * 64, image=image)


def no_device(*a):
    raise RuntimeError("tests never open a port")


def wait_until(app, cond, timeout=5.0) -> bool:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    app.processEvents()
    return cond()


# ── labels: pure ───────────────────────────────────────────────────────
@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed")
class TestCompatText(unittest.TestCase):
    def test_six_cases_and_no_banner(self):
        need = labels.UPDATE_NEEDED
        cases = {
            "api lower": (Compat("binary", 0, FW), need, True),
            # stage-275: level 2 (stage-272) is known; only a newer level gets the note
            "api higher": (Compat("binary", labels.NEWEST_API_LEVEL + 1, FW), "older than the firmware",
                           False),
            "no section 7": (Compat("binary", None, FW), need, True),
            "shell only": (Compat("shell_only"), need, True),
            "iap, no port": (Compat("iap"), "upgrade mode", True),
            "stock, no port": (Compat("stock"), "FNIRSI firmware", True),
        }
        for name, (c, words, offer) in cases.items():
            with self.subTest(name):
                text, wiz = labels.compat_text(c)
                self.assertIn(words, text)
                self.assertEqual(wiz, offer)
                self.assertNotRegex(text, r"0x[0-9A-Fa-f]|\b[0-9A-F]{24}\b")
        self.assertIsNone(labels.compat_text(Compat("binary", 1, FW)))
        self.assertIsNone(labels.compat_text(Compat("binary", 2, FW)))     # stage-272 firmware
        self.assertIsNone(labels.compat_text(Compat("none")))
        self.assertIsNone(labels.compat_text(None))

    def test_recommended_only_for_a_newer_bundled_image_same_api(self):
        c = Compat("binary", 1, FW)
        self.assertIn(labels.UPDATE_RECOMMENDED, labels.compat_text(c, manifest("Nov 26 2026 10:00:00"))[0])
        self.assertIsNone(labels.compat_text(c, manifest("Nov 25 2026 23:12:35")))     # same build
        self.assertIsNone(labels.compat_text(c, manifest("Nov 24 2026 10:00:00")))     # older
        self.assertIsNone(labels.compat_text(c, manifest("Nov 26 2026 10:00:00", api=2)))
        self.assertIsNone(labels.compat_text(Compat("binary", 1, ""), manifest()))      # no build text

    def test_build_time_padded_day(self):
        self.assertEqual(labels.build_time("OpenScope Nov  5 2026 01:02:03 abc").day, 5)
        self.assertEqual(labels.build_time("Nov 25 2026 23:12:35").hour, 23)
        self.assertIsNone(labels.build_time("no time"))

    def test_info_lines_show_section_7(self):
        st = proto.Status(1, 0, 90, 0, 4100, 1000, 0, 0, FW)
        rows = dict(labels.info_lines(FW, "COM7", st, None, proto.Description(version=1, identity=IDENT)))
        self.assertEqual(rows["Unit ID"], IDENT.uid_hex)
        self.assertEqual((rows["Build target"], rows["Git revision"]), ("guest-coldtrace", "cb8a172"))
        self.assertTrue(rows["API level"].startswith("1"))
        rows = dict(labels.info_lines(FW, "COM7", st, None, None))
        self.assertEqual(rows["Unit ID"], "not reported by this firmware")

    def test_note_words_hide_a_unit_id(self):
        self.assertNotIn(IDENT.uid_hex, labels.note_words(f"unit {IDENT.uid_hex} differs"))


# ── worker: compat report, drive poll, backup job, offline ─────────────
class DescDevice(FakeDevice):
    def __init__(self, ident=IDENT, **kw):
        super().__init__(**kw)
        self.ident = ident

    def description(self):
        self.calls.append("description")
        return proto.Description(version=1, identity=self.ident)

    def status(self):
        self.calls.append("status")
        return proto.Status(1, 2, 90, 0, 4100, 1000, 0, 0, FW)     # mode 2: no capture polls


class Drv:
    def __init__(self, kind):
        self.kind = kind


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed")
class TestWorkerCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def make(self, opener, **kw):
        w = Worker(opener, status_s=0.0, retry_s=0.0, idle_s=0.0, meter_s=0.0, nak_s=0.0, **kw)
        self.compat = []
        w.compat.connect(self.compat.append)
        return w

    def test_api_level_at_connect(self):
        w = self.make(Opener(DescDevice()))
        w.step()
        self.assertEqual(self.compat, [Compat("binary", 1, FW)])
        w.step()
        self.assertEqual(len(self.compat), 1)          # reported once per connection

    def test_no_section_7(self):
        w = self.make(Opener(DescDevice(ident=None)))
        w.step()
        self.assertEqual(self.compat, [Compat("binary", None, FW)])

    def test_drive_poll_every_2_s_while_no_port(self):
        now = [0.0]
        drives = [[Drv("OTHER")]]
        calls = []

        def probe():
            calls.append(now[0])
            return drives[0]
        w = self.make(Opener(NoDevice("no port")), drive_probe=probe, clock=lambda: now[0])
        w.step()
        self.assertEqual(self.compat, [Compat("none")])
        drives[0] = [Drv("IAP")]
        now[0] = 1.0
        w.step()
        self.assertEqual(len(calls), 1)                # not yet 2 s
        now[0] = 2.0
        w.step()
        self.assertEqual(self.compat[-1], Compat("iap"))
        drives[0] = [Drv("STOCK_SHARING")]
        now[0] = 4.0
        w.step()
        self.assertEqual(self.compat[-1], Compat("stock"))
        self.assertEqual(calls, [0.0, 2.0, 4.0])

    def test_shell_only_probe_once(self):
        probes = []

        def fprobe(port):
            probes.append(port)
            return "shell_only"
        w = self.make(Opener(Timeout("no reply to STATUS")), firmware_probe=fprobe,
                      drive_probe=lambda: [])
        for _ in range(4):
            w.step()
        self.assertEqual(self.compat, [Compat("shell_only")])
        self.assertEqual(len(probes), 1)

    def test_busy_port_polls_nothing(self):
        polls = []
        w = self.make(Opener(NoDevice("COM7 is in use by another process")),
                      drive_probe=lambda: polls.append(1) or [])
        w.step()
        self.assertEqual((polls, self.compat), ([], []))

    def test_backup_job_uses_the_library(self):
        from openscope import calbackup as cb
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        folder = tmp / IDENT.uid_hex / "2026-11-25_120000"
        got = {}

        def fake_make(dev, root=None, progress=None, cancel=None, **kw):
            got["root"] = root
            progress("cal", 1, 2)
            return folder
        res, prog = [], []
        orig = (cb.make_backup, cb.verify_backup, cb.read_info)
        cb.make_backup = fake_make
        cb.verify_backup = lambda f: cb.VerifyResult(True, ())
        cb.read_info = lambda f: {"unit": {"uid": IDENT.uid_hex}, "cal": {"block_crc32": "0x1"},
                                  "files": {cb.CAL_FILE: {"crc32": "0x2"}}}
        try:
            w = self.make(Opener(DescDevice()))
            w.backup_result.connect(res.append)
            w.backup_progress.connect(lambda *a: prog.append(a))
            w.step()
            w.backup(tmp)
            w.step()
        finally:
            cb.make_backup, cb.verify_backup, cb.read_info = orig
        self.assertEqual(got["root"], tmp)
        self.assertEqual(prog, [("cal", 1, 2)])
        self.assertTrue(res[0]["verified"])
        self.assertEqual(res[0]["uid"], IDENT.uid_hex)


# ── the main window banner ─────────────────────────────────────────────
@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed")
class TestBanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.worker = Worker(no_device)
        self.win = MainWindow(self.worker, settings=QtCore.QSettings(os.path.join(self.tmp, "g.ini"),
                                                                     QtCore.QSettings.IniFormat))
        self.win._bundled = False           # no bundled image in these tests
        self.win.show()

    def tearDown(self):
        self.win.timer.stop()
        self.win.rate_timer.stop()
        self.win.hide()
        self.win.deleteLater()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_banner_per_case(self):
        w = self.win
        for c, offer in ((Compat("binary", 0, FW), True), (Compat("binary", None, FW), True),
                         (Compat("shell_only"), True), (Compat("iap"), True), (Compat("stock"), True),
                         (Compat("binary", labels.NEWEST_API_LEVEL + 1, FW), False)):
            with self.subTest(c.kind, api=c.api_level):
                self.worker.compat.emit(c)
                self.assertTrue(w.compat_banner.isVisibleTo(w))
                self.assertEqual(w.compat_label.text(), labels.compat_text(c)[0])
                self.assertEqual(w.compat_button.isVisibleTo(w), offer)
                self.assertEqual(w.compat_button.text(), "Update firmware…")
        for level in (labels.REQUIRED_API_LEVEL, labels.NEWEST_API_LEVEL):
            self.worker.compat.emit(Compat("binary", level, FW))
            self.assertFalse(w.compat_banner.isVisibleTo(w))
        self.worker.compat.emit(Compat("iap"))
        w._set_conn("offline", "")
        self.assertFalse(w.compat_banner.isVisibleTo(w))

    def test_device_menu_items(self):
        dev = [m for m in self.win.menuBar().actions() if m.text() == "&Device"][0].menu()
        texts = [a.text() for a in dev.actions()]
        i = texts.index("Device information…")
        self.assertEqual(texts[i + 1:i + 3], ["Back up calibration…", "Update firmware…"])

    def test_menu_and_banner_open_the_wizard(self):
        # hand check 2026-11-25: the wizard's own `finished` attribute hid the
        # QDialog signal, so neither the menu item nor the button opened it.
        w = self.win
        self.worker.compat.emit(Compat("binary", 0, FW))
        for how in (w.act_update.trigger, w.compat_button.click):
            with self.subTest(how=how):
                how()
                self.app.processEvents()
                self.assertIsNotNone(w.wizard)
                self.assertTrue(w.wizard.isVisible())
                self.assertEqual(w.wizard.currentId(), uw.CHECK)
                w.wizard.reject()
                self.app.processEvents()
                self.assertIsNone(w.wizard)
                self.assertTrue(self.worker.online)

    def test_menu_opens_the_backup_dialog(self):
        w = self.win
        w.act_backup.trigger()
        self.app.processEvents()
        dlgs = [d for d in w.findChildren(uw.BackupDialog) if d.isVisible()]
        self.assertEqual(len(dlgs), 1)
        self.assertIn("not connected", dlgs[0].text.text())
        dlgs[0].reject()

    def test_backup_dialog_when_not_connected_queues_nothing(self):
        dlg = uw.BackupDialog(self.worker, False, self.win)
        self.assertTrue(self.worker._jobs.empty())
        self.assertIn("not connected", dlg.text.text())
        dlg.done(0)


# ── the wizard with a fake flow ────────────────────────────────────────
class FakeFlow:
    """A library flow with the real Event shapes. hold_backup: stay in the
    backup step until cancel (no verified backup). The copy step waits for
    release_copy."""

    def __init__(self, hold_backup=False):
        self.hold_backup = hold_backup
        self.release_copy = threading.Event()
        self.in_copy = threading.Event()
        self.copies = 0
        self.kwargs = None
        self.on_copy = None

    def __call__(self, data, manifest, name, *, report, cancel, **kw):
        self.kwargs = kw
        E = up.Event
        report(E("step", "check", "checking the image"))
        if "install_ack" not in kw:
            report(E("step", "backup", "backing up"))
            report(E("progress", "backup", "cal", 1, 4))
            while self.hold_backup:
                if cancel():
                    raise Cancelled("backup cancelled")
                time.sleep(0.005)
            report(E("info", "backup", "backup X verified, unit ID = section 7"))
        report(E("step", "enter", "upgrade mode"))
        report(E("wait", "enter", up.ENTER_TEXT))
        if cancel():
            raise Cancelled("cancelled by the user before the copy; nothing was written")
        report(E("step", "drive", "checking the drive"))
        report(E("step", "copy", "copying the image"))
        self.copies += 1
        if self.on_copy:
            self.on_copy()
        self.in_copy.set()
        self.release_copy.wait(10)
        report(E("step", "wait", "waiting for the unit to restart"))
        report(E("step", "verify", "checking"))
        report(E("step", "log", "writing the update log"))
        report(E("done", "log", "update complete"))
        return up.UpdateResult(ok=True, path="F" if "install_ack" in kw else "A",
                               compare_lines=["cal block 0x025-0x12E identical"], seconds=40.0)


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed")
class WizardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        img = self.tmp / "guest-coldtrace-abc1234.bin"
        img.write_bytes(b"\x00" * 16)
        manifest().save(fw.manifest_path_for(img))
        self.images = uw.find_images(self.tmp)
        self.worker = Worker(no_device)         # never started: offline at once
        self.wiz = None

    def tearDown(self):
        if self.wiz is not None:
            self.wiz.locked = False
            for f in getattr(self, "flows", []):
                f.release_copy.set()
            if self.wiz.runner is not None:
                self.wiz.runner.cancel()
                self.wiz.runner.wait(5000)
            self.wiz.hide()
            self.wiz.deleteLater()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, compat, flow, **kw):
        self.flows = [flow]
        kw.setdefault("run_update", flow)
        kw.setdefault("run_first_install", flow)
        kw.setdefault("check_image", lambda d, m: [])
        self.wiz = uw.UpdateWizard(self.worker, compat, proto.Description(version=1, identity=IDENT), FW,
                                   images=self.images,
                                   backup_root=self.tmp / "b", offline_poll_ms=1, **kw)
        self.wiz.show()
        self.app.processEvents()
        return self.wiz

    def until(self, cond, timeout=5.0):
        self.assertTrue(wait_until(self.app, cond, timeout))


class TestWizard(WizardCase):
    def test_check_page_blocks_a_refused_image(self):
        w = self.make(Compat("binary", 1, FW), FakeFlow(), check_image=lambda d, m: ["bad SP"])
        self.assertEqual(w.currentId(), uw.CHECK)
        self.assertFalse(w.currentPage().isComplete())
        self.assertIn("bad SP", w.pages[uw.CHECK].image.text())
        self.assertIn(IDENT.uid_hex, w.pages[uw.CHECK].unit.text())     # dialogs may show it
        w.next()                                    # QWizard.next() ignores isComplete()
        self.assertEqual(w.currentId(), uw.CHECK)
        self.assertIsNone(w.runner)
        self.assertTrue(self.worker.online)

    def test_check_page_needs_a_unit(self):
        w = self.make(Compat("none"), FakeFlow())
        self.assertFalse(w.currentPage().isComplete())
        self.worker.compat.emit(Compat("iap"))      # the worker finds the drive
        self.app.processEvents()
        self.assertTrue(w.currentPage().isComplete())
        self.assertEqual(w.path, "C")

    def test_no_backup_page_2_cannot_go_on(self):
        flow = FakeFlow(hold_backup=True)
        w = self.make(Compat("binary", 1, FW), flow)
        w.next()
        self.assertEqual(w.currentId(), uw.BACKUP)
        self.until(lambda: "backup" in w.steps)
        self.assertFalse(w.worker.online)           # offline before the flow opens the port
        time.sleep(0.05)
        self.app.processEvents()
        self.assertFalse(w.currentPage().isComplete())
        w.next()
        self.assertEqual(w.currentId(), uw.BACKUP)
        self.assertEqual(flow.copies, 0)
        w.reject()                                  # cancel before the copy: allowed
        self.assertFalse(w.isVisible())
        self.assertTrue(self.worker.online)         # the worker is online again
        self.assertEqual(flow.copies, 0)

    def test_backup_verified_page_2_goes_on(self):
        flow = FakeFlow()
        w = self.make(Compat("binary", 1, FW), flow)
        w.next()
        self.until(lambda: w.gate_step == "enter")
        self.assertTrue(w.currentPage().isComplete())
        self.assertNotIn("enter", w.steps)          # the flow waits for Next
        self.assertEqual(flow.copies, 0)
        w.next()
        self.assertEqual(w.currentId(), uw.ENTER)
        self.until(lambda: w.currentId() == uw.COPY)
        self.until(lambda: flow.in_copy.is_set())
        flow.release_copy.set()
        self.until(lambda: w.currentId() == uw.RESULT)
        head, lines = w.result_text()
        self.assertEqual(head, labels.WIZ["ok"])
        self.assertTrue(any("identical" in x for x in lines))
        self.assertTrue(any(x.startswith("Pages:") for x in lines))
        self.assertEqual(flow.copies, 1)
        self.assertEqual(flow.kwargs["backup_root"], self.tmp / "b")
        w.accept()
        self.assertTrue(self.worker.online)

    def test_first_install_needs_the_acknowledgment(self):
        flow = FakeFlow()
        w = self.make(Compat("stock"), flow)
        w.next()
        page = w.pages[uw.BACKUP]
        self.assertTrue(page.ack.isVisibleTo(w) and page.typed.isVisibleTo(w))
        self.assertIsNone(w.runner)                 # nothing starts before the acknowledgment
        self.assertFalse(page.isComplete())
        page.ack.setChecked(True)
        self.assertFalse(page.isComplete())
        page.typed.setText("install")
        self.assertFalse(page.isComplete())
        page.typed.setText(up.FIRST_INSTALL_ACK)
        self.assertTrue(page.isComplete())
        page.ack.setChecked(False)
        self.assertFalse(page.isComplete())
        page.ack.setChecked(True)
        w.next()
        self.until(lambda: flow.in_copy.is_set())
        self.assertEqual((flow.kwargs["model"], flow.kwargs["install_ack"]), (up.MODEL, "INSTALL"))
        flow.release_copy.set()
        self.until(lambda: w.currentId() == uw.RESULT)

    def test_copy_page_has_no_cancel_and_cannot_close(self):
        flow = FakeFlow()
        w = self.make(Compat("binary", 1, FW), flow)
        w.next()
        self.until(lambda: w.gate_step is not None)
        w.next()
        self.until(lambda: flow.in_copy.is_set() and w.currentId() == uw.COPY)
        self.assertFalse(w.button(QtWidgets.QWizard.CancelButton).isVisible())
        self.assertFalse(w.button(QtWidgets.QWizard.BackButton).isVisible())
        w.reject()
        self.assertTrue(w.isVisible())
        w.close()
        self.app.processEvents()
        self.assertTrue(w.isVisible())
        self.assertFalse(self.worker.online)        # still offline during the write
        flow.release_copy.set()
        self.until(lambda: w.currentId() == uw.RESULT)
        self.assertFalse(w.locked)
        w.accept()
        self.assertFalse(w.isVisible())

    def test_refusal_goes_to_the_result_page(self):
        def refused(*a, report, cancel, **kw):
            report(up.Event("step", "check", "checking the image"))
            report(up.Event("step", "backup", "backing up"))
            raise up.Refused("backup failed, nothing will be copied: no answer", "backup")
        w = self.make(Compat("binary", 1, FW), FakeFlow(), run_update=refused)
        w.next()
        self.until(lambda: w.currentId() == uw.RESULT)
        self.assertEqual(w.pages[uw.RESULT].head.text(), labels.WIZ["refused"])

    def test_path_c_skips_the_backup_and_gates_the_copy(self):
        events = []

        def path_c(*a, report, cancel, **kw):
            report(up.Event("step", "check", "checking the image"))
            report(up.Event("step", "drive", "no OpenScope port"))
            report(up.Event("step", "backup", "looking for a backup of this unit"))
            report(up.Event("info", "backup", "backup B matches USB serial S, verified"))
            report(up.Event("step", "copy", "copying the image"))
            events.append("copied")
            report(up.Event("step", "wait", "waiting"))
            return up.UpdateResult(ok=True, path="C")
        w = self.make(Compat("iap"), FakeFlow(), run_update=path_c)
        w.next()
        self.until(lambda: w.gate_step == "copy")
        self.assertEqual(events, [])                 # no copy before Next
        w.next()
        self.until(lambda: w.currentId() == uw.RESULT)
        self.assertEqual(events, ["copied"])

    def test_pictures_exist_and_load(self):
        from PySide6 import QtGui
        for n in ("upgrade_menu_power.svg", "upgrade_menu_pinhole.svg"):
            self.assertFalse(QtGui.QPixmap(str(uw.PICTURES / n)).isNull(), n)


class TestWorkerQuietDuringUpdate(WizardCase):
    def test_worker_sends_nothing_while_the_runner_works(self):
        dev = DescDevice()
        self.worker = Worker(Opener(dev), status_s=0.0, retry_s=0.01, idle_s=0.001, drive_probe=lambda: [])
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.until(lambda: len(dev.calls) > 20)
        counts = {}
        flow = FakeFlow()
        flow.on_copy = lambda: counts.setdefault("at_copy", len(dev.calls))
        w = self.make(Compat("binary", 1, FW), flow)
        w.next()
        self.until(lambda: w.gate_step is not None)
        self.assertTrue(dev.closed)                 # the port is free before the flow runs
        counts["start"] = len(dev.calls)
        w.next()
        self.until(lambda: flow.in_copy.is_set())
        time.sleep(0.2)
        self.app.processEvents()
        counts["during"] = len(dev.calls)
        flow.release_copy.set()
        self.until(lambda: w.currentId() == uw.RESULT)
        self.assertEqual(counts["start"], counts["during"])
        self.assertEqual(counts["start"], counts["at_copy"])
        w.accept()
        self.until(lambda: len(dev.calls) > counts["during"])      # online again: polls again


# ── the real library flow (path A) through the wizard ──────────────────
def _library_tests() -> "Path | None":
    import openscope
    d = Path(openscope.__file__).resolve().parents[1] / "tests"
    return d if (d / "test_update.py").is_file() else None


@unittest.skipUnless(HAVE_QT and _library_tests() is not None,
                     "PySide6 missing or the library tests are not next to the library")
class TestRealFlow(WizardCase):
    def test_path_a_with_the_library_fake_unit(self):
        from unittest import mock
        sys.path.insert(0, str(_library_tests()))
        import test_update as tu
        import test_calbackup as tcb
        clock = tcb.FakeClock()
        p = mock.patch("openscope.device.time", clock)
        p.start()
        self.addCleanup(p.stop)
        unit = tu.UpdUnit()
        bench = tu.Bench(self.tmp, unit, clock)
        real_probe = bench.probe

        entered = []

        def probe():                                # the user enters upgrade mode at once
            if not entered:
                entered.append(1)
                bench.state = "iap"
            return real_probe()
        hooks = bench.hooks()
        hooks.probe = probe
        img = tu.make_image()
        m = tu.manifest_for(img)
        d = self.tmp / "real"
        d.mkdir()
        (d / "guest-coldtrace-abc1234.bin").write_bytes(img)
        m.save(fw.manifest_path_for(d / "guest-coldtrace-abc1234.bin"))
        self.images = uw.find_images(d)
        self.wiz = uw.UpdateWizard(self.worker, Compat("binary", 1, FW),
                                   proto.Description(version=1, identity=unit.ident), FW,
                                   images=self.images, hooks=hooks, backup_root=self.tmp / "b",
                                   offline_poll_ms=1)
        w = self.wiz
        w.show()
        self.app.processEvents()
        self.assertTrue(w.currentPage().isComplete(), w.pages[uw.CHECK].image.text())
        w.next()
        self.until(lambda: w.gate_step == "enter" or w.failed, 20)
        self.assertFalse(w.failed, w.err)
        self.assertFalse(bench.copied())            # backup verified, nothing copied yet
        w.next()
        self.until(lambda: w.currentId() == uw.RESULT, 30)
        self.assertIsNone(w.err, w.err)
        self.assertTrue(w.res.ok, w.res.problems)
        self.assertTrue(bench.copied())
        self.assertEqual(w.pages[uw.RESULT].head.text(), labels.WIZ["ok"])


if __name__ == "__main__":
    unittest.main()
