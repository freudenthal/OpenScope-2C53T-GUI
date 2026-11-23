"""gui window (stage-087), offscreen: the window with a real Worker that is
never started, so queued jobs can be inspected and no port is touched. Each
test uses its own temporary QSettings file. Needs the `gui` extra; skipped
without it."""
from __future__ import annotations

import os
import queue
import re
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openscope import proto  # noqa: E402

try:
    import numpy as np  # noqa: F401
    from PySide6 import QtCore, QtWidgets
    from openscope.gui.app import MainWindow  # noqa: E402
    from openscope.gui.window import SETTINGS_BANNER  # noqa: E402
    from openscope.gui.worker import ScopeFrame, Worker, shell_refused  # noqa: E402
    HAVE_QT = True
except ImportError:          # pragma: no cover - depends on the venv
    HAVE_QT = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gui_labels import REAL_DESCRIPTION  # noqa: E402

HEX = re.compile(r"0x[0-9A-Fa-f]")
# Unit key names. CH1/CH2 are left out: they are also the channel names
# printed at the unit's inputs, and the GUI must name the channels.
KEY_NAME = re.compile(r"\b(MOVE|SELECT|TRIGGER|PRM|AUTO|SAVE|MENU|OK|UP|DOWN|LEFT|RIGHT|POWER)\b")


def state(running=True, tb=0x10, mode=0, r1=7, r2=7, tmode=0, edge=0, src=0, level=0, probe=(10, 1)):
    ch = (proto.ScopeChannel(True, r1, 2, 26030850), proto.ScopeChannel(True, r2, 2, 2466778))
    return proto.ScopeState(1, mode, running, tb, tb, 2, tmode, edge, src, level, 12490, 0x80, 32, ch,
                            probe)


def status(mode=0):
    return proto.Status(1, mode, 90, proto.FLAG_CAPTURE_READY, 4100, 1000, 0, 0, "fake fw")


def frame(tb=0x10, r1=7, fid=7):
    w = proto.Waveform(fid, 0, proto.WAVE_FLAG_TB_MEASURED, tb, r1, 1024, 24, 12490, 26030850, 32, 24,
                       bytes(range(256)) * 4)
    return ScopeFrame.from_waves((w,), 0.0)


def no_device():
    raise RuntimeError("tests never open a port")


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ini = os.path.join(self.tmp, "gui.ini")
        self.worker = Worker(no_device)
        self.win = self.make()
        self.desc = proto.parse_description(REAL_DESCRIPTION)

    def make(self):
        w = MainWindow(self.worker, settings=QtCore.QSettings(self.ini, QtCore.QSettings.IniFormat))
        w.show()            # dock toggle actions follow real visibility (offscreen platform)
        return w

    def tearDown(self):
        for w in getattr(self, "_wins", []) + [self.win]:
            w.timer.stop()
            w.rate_timer.stop()
            w.hide()
            w.deleteLater()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jobs(self):
        out = []
        while True:
            try:
                out.append(self.worker._jobs.get_nowait())
            except queue.Empty:
                return out

    def connect(self, mode=0, st=None):
        w = self.win
        w._set_conn("connected", "COMFAKE")
        w._description(self.desc)
        w._status(status(mode))
        w._scope_state(st or state(mode=mode))


class TestModesAndTabs(WindowCase):
    def test_tab_click_queues_one_set_mode(self):
        self.connect(mode=0)
        self.assertEqual(self.jobs(), [])
        self.win.tabs.setCurrentIndex(1)                # what a click on the tab does
        self.assertEqual(self.jobs(), [("set", ("mode", ("meter",)))])
        self.assertEqual(self.win.stack.currentIndex(), 1)
        # a STATUS taken before the set carries the old mode: the tab must not jump back
        self.win._status(status(0))
        self.assertEqual(self.win.tabs.currentIndex(), 1)
        self.win._set_done("mode", True)
        self.win._status(status(1))
        self.assertEqual(self.win.tabs.currentIndex(), 1)
        self.assertEqual(self.jobs(), [])

    def test_unit_mode_change_moves_the_tab_and_sends_nothing(self):
        self.connect(mode=0)
        self.win._status(status(1))
        self.assertEqual((self.win.tabs.currentIndex(), self.win.stack.currentIndex()), (1, 1))
        self.win._scope_state(state(mode=2))
        self.assertEqual(self.win.tabs.currentIndex(), 2)
        self.assertEqual(self.jobs(), [])

    def test_failed_set_mode_goes_back_to_the_unit_tab(self):
        self.connect(mode=0)
        self.win.tabs.setCurrentIndex(2)
        self.jobs()
        self.win._set_done("mode", False)
        self.assertEqual(self.win.tabs.currentIndex(), 0)
        self.assertEqual(self.jobs(), [])

    def test_settings_mode_shows_the_banner_and_keeps_the_tab(self):
        self.connect(mode=1)
        self.win._status(status(3))
        self.assertTrue(self.win.banner.isVisibleTo(self.win))
        self.assertEqual(self.win.banner.text(), SETTINGS_BANNER)
        self.assertEqual(self.win.tabs.currentIndex(), 1)
        self.assertEqual(self.jobs(), [])
        self.win._status(status(1))
        self.assertFalse(self.win.banner.isVisibleTo(self.win))

    def test_no_set_while_not_connected(self):
        self.win.tabs.setCurrentIndex(1)
        self.assertEqual(self.jobs(), [])

    def test_tabs_are_modes_in_words(self):
        self.assertEqual([self.win.tabs.tabText(i) for i in range(3)],
                         ["Oscilloscope", "Multimeter", "Signal generator"])


class TestScopePanel(WindowCase):
    def test_disabled_until_a_state_arrives(self):
        p = self.win.panel
        self.assertFalse(p.settings.isEnabled())
        self.connect()
        self.assertTrue(p.settings.isEnabled())
        self.win._set_conn("searching", "")
        self.assertFalse(p.settings.isEnabled())

    def test_a_state_fills_practical_items_and_sends_nothing(self):
        self.connect(st=state(running=False, tb=0x0F, r1=6, r2=5, tmode=2, edge=1, src=1, level=-40))
        p = self.win.panel
        self.assertEqual(p.tb_cb.currentText(), "1.28 ms/div")
        self.assertIn("0x0F", p.tb_cb.toolTip())
        self.assertEqual(p.vdiv_cb[0].currentText(), "12.6 V/div")        # probe x10 included
        self.assertEqual(p.vdiv_cb[1].currentText(), "617 mV/div")
        self.assertEqual(p.probe_cb[0].currentText(), "×10")
        self.assertEqual((p.tmode_cb.currentText(), p.tedge_cb.currentText(), p.tsrc_cb.currentText()),
                         ("Single", "Falling edge", "CH2"))
        self.assertEqual(p.level_sb.suffix(), " V")
        self.assertEqual(p.level(), -40)
        self.assertEqual(p.run_btn.text(), "Run")
        self.assertIn("Single capture", p.state_lbl.text())      # trigger Single: not "Stopped"
        self.assertEqual(self.jobs(), [])

    def test_each_user_change_queues_exactly_one_set(self):
        self.connect()
        p = self.win.panel
        p.tb_cb.activated.emit(p.tb_cb.findData(0x14))
        p.vdiv_cb[1].activated.emit(p.vdiv_cb[1].findData(5))
        p.probe_cb[0].activated.emit(p.probe_cb[0].findData(1))
        p.run_btn.click()
        p.tmode_cb.setCurrentIndex(1)
        p.tmode_cb.activated.emit(1)
        self.assertEqual(self.jobs(), [("set", ("timebase", (0x14,))), ("set", ("vdiv", (2, 5))),
                                       ("set", ("probe", (1, 1))), ("set", ("run", (False,))),
                                       ("set", ("trigger", ("normal", "rising", "CH1", 0)))])

    def test_single_is_trigger_single_then_run(self):
        self.connect(st=state(running=False, edge=1, src=1, level=12))
        self.win.panel.single_btn.click()
        self.assertEqual(self.jobs(), [("set", ("trigger", ("single", "falling", "CH2", 12))),
                                       ("set", ("run", (True,)))])

    def test_run_after_single_returns_to_continuous(self):
        """After Single the unit reports running while it holds one record; Run must
        put the previous trigger mode back, not re-arm another single (user report)."""
        self.connect(st=state(tmode=0, level=29))
        p = self.win.panel
        self.assertEqual(p.run_btn.text(), "Stop")
        p.single_btn.click()
        self.jobs()
        self.win._scope_state(state(running=True, tmode=2, level=29))
        self.assertEqual(p.run_btn.text(), "Run")
        self.assertIn("Single capture", p.state_lbl.text())
        p.run_btn.click()
        self.assertEqual(self.jobs(), [("set", ("trigger", ("auto", "rising", "CH1", 29))),
                                       ("set", ("run", (True,)))])
        self.win._scope_state(state(running=True, tmode=0, level=29))
        self.assertEqual(p.run_btn.text(), "Stop")

    def test_run_after_single_restores_normal(self):
        self.connect(st=state(tmode=1))
        self.win._scope_state(state(running=False, tmode=2))         # single, then stopped
        self.win.act_run.trigger()                                   # toolbar / Space: same path
        self.assertEqual(self.jobs(), [("set", ("trigger", ("normal", "rising", "CH1", 0))),
                                       ("set", ("run", (True,)))])

    def test_unit_already_in_single_resumes_auto(self):
        self.connect(st=state(tmode=2))
        self.win.panel.run_btn.click()
        self.assertEqual(self.jobs()[0], ("set", ("trigger", ("auto", "rising", "CH1", 0))))

    def test_single_record_note(self):
        self.connect(st=state(tmode=2))
        self.win._frame(frame())
        self.assertIn("single record", self.win.sb_scope.text())

    def test_level_in_volts_sent_only_when_it_changed(self):
        self.connect(st=state(level=10))
        p = self.win.panel
        p.level_sb.editingFinished.emit()
        self.assertEqual(self.jobs(), [])
        p.level_sb.setValue(p._lscale.value(-25))
        p.level_sb.editingFinished.emit()
        self.assertEqual(self.jobs(), [("set", ("trigger", ("auto", "rising", "CH1", -25)))])

    def test_uncalibrated_items_hidden_by_default(self):
        self.connect()
        p = self.win.panel
        self.assertEqual(p.tb_cb.count(), 8)
        self.win.act_uncal.setChecked(True)
        self.assertEqual(p.tb_cb.count(), 21)
        self.assertEqual(p.tb_cb.currentText(), "2.56 ms/div")
        self.assertEqual(self.jobs(), [])

    def test_held_record_note_in_words(self):
        self.connect()
        self.win._frame(frame(tb=0x10, r1=7))
        self.assertNotIn("held", self.win.sb_scope.text())
        self.win._scope_state(state(running=False, tb=0x0F, r1=7))     # no new frame under STOP
        txt = self.win.sb_scope.text()
        self.assertIn("held record", txt)
        self.assertIn("record taken at 2.56 ms/div (now 1.28 ms/div)", txt)


class TestNoCodesVisible(WindowCase):
    def visible_texts(self):
        w = self.win
        out = []
        for cls in (QtWidgets.QLabel, QtWidgets.QAbstractButton, QtWidgets.QComboBox,
                    QtWidgets.QGroupBox, QtWidgets.QAbstractSpinBox, QtWidgets.QDockWidget):
            for x in w.findChildren(cls):
                if not x.isVisibleTo(w):
                    continue
                if isinstance(x, QtWidgets.QComboBox):
                    out += [x.itemText(i) for i in range(x.count())]
                elif isinstance(x, QtWidgets.QAbstractSpinBox):
                    out.append(x.text())
                elif isinstance(x, QtWidgets.QGroupBox):
                    out.append(x.title())
                elif isinstance(x, QtWidgets.QDockWidget):
                    out.append(x.windowTitle())
                else:
                    out.append(x.text())
        out += [w.tabs.tabText(i) for i in range(w.tabs.count())]
        out += [a.text() for a in w.menuBar().actions()]
        out.append(w.statusBar().currentMessage())
        return out

    def test_no_hex_and_no_key_name_with_default_view(self):
        self.connect()
        self.win._frame(frame())
        self.win._note("timeout: no reply to 0x22 within 1.0 s")
        self.win._job_done("set", True, "timebase 16")
        texts = self.visible_texts()
        self.assertGreater(len(texts), 40)
        self.assertEqual([t for t in texts if HEX.search(t)], [])
        self.assertEqual([t for t in texts if KEY_NAME.search(t)], [])
        self.assertTrue(any("2.56 ms/div" in t for t in texts))

    def test_keys_dock_shows_key_names_only_when_asked(self):
        self.assertTrue(self.win.keys_dock.isHidden())
        self.win.act_keys.trigger()        # a menu click
        self.assertFalse(self.win.keys_dock.isHidden())
        self.assertTrue([t for t in self.visible_texts() if KEY_NAME.search(t)])


class TestViewSettings(WindowCase):
    def test_defaults(self):
        w = self.win
        self.assertTrue(w.keys_dock.isHidden())
        self.assertFalse(w.control_dock.isHidden())
        self.assertFalse(w.act_uncal.isChecked())
        self.assertFalse(w.tools_menu.menuAction().isVisible())
        self.assertEqual(w.keys_dock.windowTitle(), "Unit keys (sends key presses)")

    def test_view_settings_persist(self):
        w = self.win
        w.act_keys.trigger()                # menu clicks (dock toggle actions act on triggered)
        w.act_control.trigger()
        w.act_uncal.setChecked(True)
        w.act_advanced.setChecked(True)
        w.resize(700, 520)             # inside the offscreen screen (800x600)
        size = (w.width(), w.height())
        w.save_settings()
        w2 = self.make()
        self._wins = [w2]
        self.assertFalse(w2.keys_dock.isHidden())
        self.assertTrue(w2.control_dock.isHidden())
        self.assertTrue(w2.act_uncal.isChecked())
        self.assertTrue(w2.panel.show_uncalibrated)
        self.assertTrue(w2.tools_menu.menuAction().isVisible())
        self.assertEqual((w2.width(), w2.height()), size)
        self.assertEqual(w2.height(), 520)


class TestFunctions(WindowCase):
    def test_save_waveform_csv_and_npz(self):
        self.connect()
        self.win._frame(frame())
        p = self.win.save_waveform(os.path.join(self.tmp, "w"), "CSV (*.csv)")
        self.assertTrue(p.endswith(".csv"))
        with open(p) as f:
            self.assertEqual(sum(1 for _ in f), 1025)
        p = self.win.save_waveform(os.path.join(self.tmp, "w.npz"))
        import numpy as np
        self.assertEqual(np.load(p)["samples"].shape, (1, 1, 1024))

    def test_debug_console_refuses_i40(self):
        for line in ("cal restore", "CAL  restore x", "mem write 0x20000000 1"):
            self.assertTrue(shell_refused(line))
            with self.assertRaises(ValueError):
                self.worker.shell(line)
        self.assertFalse(shell_refused("version"))
        self.assertEqual(self.jobs(), [])

    def test_record_meter_menu_follows_the_meter_button(self):
        self.win.meter.log_btn.blockSignals(True)
        self.win.meter.log_btn.setChecked(True)
        self.win.meter.log_btn.blockSignals(False)
        self.win._meter_log_toggled(True)
        self.assertTrue(self.win.act_meter_log.isChecked())

    def test_disconnect_menu(self):
        self.win.act_disconnect.trigger()
        self.assertFalse(self.worker.online)
        self.win._set_conn("offline", "")
        self.assertTrue(self.win.act_connect.isEnabled())
        self.win.act_connect.trigger()
        self.assertTrue(self.worker.online)


if __name__ == "__main__":
    unittest.main()
