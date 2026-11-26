"""gui window (stage-087), offscreen: the window with a real Worker that is
never started, so queued jobs can be inspected and no port is touched. Each
test uses its own temporary QSettings file. Needs the `gui` extra; skipped
without it."""
from __future__ import annotations

import dataclasses
import importlib.util
import os
import queue
import re
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)
from openscope import proto  # noqa: E402

# Stage-180: only a missing PySide6/numpy/pyqtgraph skips; an import error
# inside openscope_gui fails the suite.
HAVE_QT = all(importlib.util.find_spec(m) is not None for m in ("PySide6", "numpy", "pyqtgraph"))
if HAVE_QT:
    import numpy as np  # noqa: F401
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
    from openscope_gui.app import MainWindow  # noqa: E402
    from openscope_gui.window import PORT_BUSY_TEXT, SETTINGS_BANNER  # noqa: E402
    from openscope_gui.worker import Event, MeterSample, ScopeFrame, Worker, shell_refused  # noqa: E402
    from openscope_gui.panels import Panel, PlaceholderPanel  # noqa: E402
    from openscope_gui import labels  # noqa: E402

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


class TestScopeModeActions(WindowCase):
    """stage-230: Run/Stop and Single only while the unit is in scope mode."""

    def test_enabled_in_scope_mode_only(self):
        w = self.win
        self.assertFalse(w.act_run.isEnabled() or w.act_single.isEnabled())   # not connected
        self.connect(mode=0)
        self.assertTrue(w.act_run.isEnabled() and w.act_single.isEnabled())
        for mode in (1, 2, 3):
            w._status(status(mode))
            self.assertFalse(w.act_run.isEnabled(), f"Run/Stop enabled in mode {mode}")
            self.assertFalse(w.act_single.isEnabled(), f"Single enabled in mode {mode}")
            self.assertIn("scope settings need scope mode", w.act_run.toolTip())
            w._scope_state(state(mode=mode))                 # a state in that mode keeps them off
            self.assertFalse(w.act_run.isEnabled())
        w._status(status(0))
        self.assertTrue(w.act_run.isEnabled() and w.act_single.isEnabled())
        self.assertNotIn("scope mode", w.act_run.toolTip())

    def test_connect_in_meter_mode_keeps_them_off(self):
        self.connect(mode=1)
        self.assertFalse(self.win.act_run.isEnabled() or self.win.act_single.isEnabled())

    def test_space_in_meter_mode_sends_nothing(self):
        w = self.win
        self.connect(mode=0)
        self.jobs()
        w._status(status(1))
        w.act_run.trigger()                                  # a disabled action does not fire
        w.act_single.trigger()
        self.assertEqual(self.jobs(), [])

    def test_refused_set_shows_scope_mode_words(self):
        w = self.win
        self.connect(mode=0)
        w._job_done("set", False, "timebase refused: UNSUPPORTED_IN_MODE: the scope is not in "
                                  "oscilloscope mode")
        self.assertIn("scope settings need scope mode", w.sb_note.text())


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

    def test_trigger_line_drag_and_level_50(self):
        self.connect(st=state(level=21))
        w, p = self.win, self.win.panel
        self.assertEqual(w.scope.trig_line.value(), 126)       # where the unit fires, in counts
        self.assertTrue(w.scope.trig_line.isVisible())
        w.scope.level_dragged.emit(55.4)                        # the user drops the line at 55
        lv = self.jobs()[0][1][1][3]
        self.assertEqual(self.desc.trigger.record_counts(lv), 55)
        w._scope_state(state(level=21))
        sq = proto.Waveform(8, 0, 0, 0x10, 7, 1024, 24, 12490, 26030850, 32, 24, bytes([10, 100]) * 512)
        w._frame(ScopeFrame.from_waves((sq,), 0.0))
        self.assertIn("trigger level outside the CH1 signal", w.sb_scope.text())
        p.level50_btn.click()
        self.assertEqual(self.jobs(), [("set", ("trigger", ("auto", "rising", "CH1", lv)))])

    def test_level_50_needs_ch1(self):
        self.connect()
        w2 = proto.Waveform(9, 1, 0, 0x10, 7, 1024, 24, 12490, 2466778, 32, 24, bytes(range(256)) * 4)
        self.win._frame(ScopeFrame.from_waves((w2,), 0.0))
        self.win.panel.level50_btn.click()
        self.assertEqual(self.jobs(), [])
        self.assertIn("Show CH1", self.win.sb_note.text())

    def test_level_50_follows_the_ch2_source(self):
        # stage-091: with source CH2 the unit compares CH2, so 50 % is the CH2 middle
        self.connect(st=state(src=1, level=0))
        w1 = proto.Waveform(9, 0, 0, 0x10, 7, 1024, 24, 12490, 26030850, 32, 24, bytes([10, 100]) * 512)
        w2 = proto.Waveform(9, 1, 0, 0x10, 7, 1024, 24, 12490, 2466778, 32, 24, bytes([112, 198]) * 512)
        self.win._frame(ScopeFrame.from_waves((w1, w2), 0.0))
        self.win.panel.level50_btn.click()
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][1][1][:3], ("auto", "rising", "CH2"))
        self.assertEqual(self.desc.trigger.record_counts(jobs[0][1][1][3]), 155)
        self.assertEqual(self.win.panel.tsrc_cb.currentText(), "CH2")

    def test_level_50_with_ch2_source_needs_ch2(self):
        self.connect(st=state(src=1))
        self.win._frame(frame())                               # CH1 only
        self.win.panel.level50_btn.click()
        self.assertEqual(self.jobs(), [])
        self.assertIn("Show CH2", self.win.sb_note.text())

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

    def test_typed_level_survives_state_updates(self):
        # code review 2026-11-23 Q-1: each SCOPE_STATE (about 2 per second) set the
        # box's decimals/step/range/suffix, and Qt wrote the text again from the
        # stored value, so a typed level was lost before Enter.
        self.connect(st=state(level=10))
        p, sb = self.win.panel, self.win.panel.level_sb
        self.win.activateWindow()
        sb.setFocus()
        self.app.processEvents()
        self.assertTrue(sb.hasFocus())
        typed = f"{p._lscale.value(-25):.{sb.decimals()}f}"
        sb.selectAll()
        QtTest.QTest.keyClicks(sb, typed)
        self.assertEqual(sb.cleanText(), typed)
        self.win._scope_state(state(level=10))                  # same scale
        self.assertEqual(sb.cleanText(), typed)
        self.win._scope_state(state(level=10, r1=6))            # CH1 range changed: new scale
        self.assertEqual(sb.cleanText(), typed)
        self.assertEqual(self.jobs(), [])
        QtTest.QTest.keyClick(sb, QtCore.Qt.Key_Return)         # editing finished
        want = p._lscale.level(float(typed))                    # volts on the range now in force
        self.assertEqual(self.jobs(), [("set", ("trigger", ("auto", "rising", "CH1", want)))])
        self.assertEqual(sb.suffix(), " V")
        self.assertAlmostEqual(sb.value(), p._lscale.value(want), places=sb.decimals())

    def test_focus_out_without_typing_sends_nothing(self):
        # Q-1 follow-on: the range changes while the box has the focus; leaving the box
        # without an edit must not read the old volts on the new range and send a set.
        self.connect(st=state(level=10))
        p, sb = self.win.panel, self.win.panel.level_sb
        self.win.activateWindow()
        sb.setFocus()
        self.app.processEvents()
        self.assertTrue(sb.hasFocus())
        self.win._scope_state(state(level=10, r1=6))
        sb.editingFinished.emit()
        self.assertEqual(self.jobs(), [])
        self.assertEqual(p.level(), 10)
        self.assertAlmostEqual(sb.value(), p._lscale.value(10), places=sb.decimals())

    def test_enter_then_focus_out_sends_one_set(self):
        # stage-260 EditSender: Enter and the focus out after it are one edit
        self.connect(st=state(level=10))
        p, sb = self.win.panel, self.win.panel.level_sb
        self.win.activateWindow()
        sb.setFocus()
        self.app.processEvents()
        sb.selectAll()
        QtTest.QTest.keyClicks(sb, f"{p._lscale.value(-25):.{sb.decimals()}f}")
        QtTest.QTest.keyClick(sb, QtCore.Qt.Key_Return)
        p.tb_cb.setFocus()                                      # focus out before the state comes back
        self.app.processEvents()
        sb.editingFinished.emit()
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

    def test_no_unit_id_or_git_revision_in_the_main_window(self):
        # stage-210: section 7 shows only in dialogs (device information, backup,
        # update). A unit ID or a git revision has no "0x": check them by value.
        ident = proto.Identity(bytes(range(12)), 1, "guest-coldtrace", "cb8a172")
        desc = proto.Description(**{**{f: getattr(self.desc, f) for f in self.desc.__dataclass_fields__},
                                    "identity": ident})
        w = self.win
        w._set_conn("connected", "COMFAKE")
        w._description(desc)
        w._status(status())
        w._scope_state(state())
        w._frame(frame())
        from openscope_gui.labels import Compat
        for c in (Compat("binary", 0, "OpenScope Nov 25 2026 23:12:35 cb8a172"), Compat("shell_only")):
            w.worker.compat.emit(c)
            texts = self.visible_texts() + [w.compat_label.text()]
            self.assertTrue(w.compat_banner.isVisibleTo(w))
            for value in (ident.uid_hex, ident.git_rev, ident.usb_serial):
                self.assertEqual([t for t in texts if value in t], [], value)
            self.assertEqual([t for t in texts if HEX.search(t)], [])
            self.assertEqual([t for t in texts if KEY_NAME.search(t)], [])

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


class TestStage180Window(WindowCase):
    """Review Q-10 and the close order (stage-180)."""

    def test_busy_port_text_in_the_status_bar(self):
        busy = "cannot open COM7: in use by another process (only one client at a time)"
        self.win._set_conn("searching", busy)
        self.assertIn(PORT_BUSY_TEXT, self.win.sb_conn.text())
        self.assertEqual(self.win.sb_conn.toolTip(), busy)
        self.win._set_conn("searching", "no OpenScope found on USB 2e3c:5740 (AT32 VCP)")
        self.assertNotIn(PORT_BUSY_TEXT, self.win.sb_conn.text())

    def test_lost_connection_clears_the_trace(self):
        self.connect()
        self.win._frame(frame())
        self.assertIsNotNone(self.win.scope.frame)
        self.win._set_conn("disconnected", "NoDevice: port lost")
        self.assertIsNone(self.win.scope.frame)
        self.assertIn("Connection lost", self.win.sb_conn.text())

    def test_port_menu_deletes_old_actions(self):
        self.win._fill_ports()
        old = list(self.win.port_group.actions())
        self.win._fill_ports()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        alive = [a for a in self.win.findChildren(QtGui.QAction) if a in old]
        self.assertEqual(alive, [], "old port actions were not deleted")
        self.assertEqual(len(self.win.port_group.actions()), len(old))

    def test_info_dialog_is_deleted_on_close(self):
        self.win._device_info()
        dlg = self.win._info_dlg
        self.assertTrue(dlg.testAttribute(QtCore.Qt.WA_DeleteOnClose))
        dlg.close()
        self.assertIsNone(self.win._info_dlg)

    def test_run_icon_is_set_only_when_run_state_changes(self):
        # A new QIcon on every SCOPE_STATE (2 Hz) grew native memory (stage-180 soak)
        self.connect()
        key = self.win.act_run.icon().cacheKey()
        calls = []
        real = self.win.act_run.setIcon
        self.win.act_run.setIcon = lambda i: (calls.append(i), real(i))
        for _ in range(5):
            self.win._scope_state(state(running=True))
        self.assertEqual(calls, [])
        self.assertEqual(self.win.act_run.icon().cacheKey(), key)
        self.win._scope_state(state(running=False))
        self.win._scope_state(state(running=False))
        self.win._scope_state(state(running=True))
        self.assertEqual(len(calls), 2)

    def test_space_does_not_auto_repeat(self):
        self.assertFalse(self.win.act_run.autoRepeat())

    def test_close_stops_timers_then_worker_then_files(self):
        stats = os.path.join(self.tmp, "s.csv")
        events = os.path.join(self.tmp, "e.txt")
        w = MainWindow(self.worker, stats, events,
                       settings=QtCore.QSettings(self.ini, QtCore.QSettings.IniFormat))
        self._wins = [w]
        w.mode_timer.start(10000)
        seen = {}

        def stop(wait_ms=5000):
            seen["timers"] = [t.isActive() for t in (w.timer, w.rate_timer, w.mode_timer)]
            seen["files_open"] = (not w._stats_f.closed, not w._events.closed)
            return True
        self.worker.stop = stop
        w.close()
        self.assertEqual(seen["timers"], [False, False, False], "a timer still ran when the worker stopped")
        self.assertEqual(seen["files_open"], (True, True), "files closed before the worker stopped")
        self.assertTrue(w._stats_f.closed and w._events.closed)
        with open(events, encoding="utf-8") as f:
            self.assertIn("closed", f.read())


class TestStage260Window(WindowCase):
    """The panel dict, the disconnect loop, the placeholders, event routing."""

    def reading(self, uc=1):
        return MeterSample(1000.0, proto.MeterReading(uc, 3.254, 0, 3, 1, 0, 0, 0, "V", "3.254"))

    def test_one_panel_per_tab_in_the_stack(self):
        w = self.win
        self.assertEqual(sorted(w.panels), [0, 1, 2])
        self.assertIs(w.panels[0], w.panel)
        for mode, p in w.panels.items():
            self.assertIsInstance(p, Panel)
            self.assertIs(w.stack.widget(mode), p)
            self.assertEqual(p.mode, mode)

    def test_placeholder_names_stage_285_and_the_meter_panel_is_real(self):
        # stage-275: the Multimeter placeholder is gone
        from openscope_gui.meter_panel import MeterPanel
        self.assertIsInstance(self.win.panels[1], MeterPanel)
        self.assertIsInstance(self.win.panels[2], PlaceholderPanel)
        self.assertIn("stage 285", self.win.panels[2].text())
        self.assertIn("stage 285", self.win.gen_view.findChild(QtWidgets.QLabel).text())
        texts = " ".join(l.text() for l in self.win.findChildren(QtWidgets.QLabel))
        self.assertNotRegex(texts, r"stage (08[89]|275)")

    def test_disconnect_clears_all_panels_and_the_meter(self):
        self.connect(mode=1)
        cleared = []
        for p in self.win.panels.values():
            p.clear = (lambda p=p: cleared.append(p.mode))
        self.win.meter.add(self.reading())
        self.win._worker_event(Event("meter_blank", False, "NOT_READY"))
        self.win._set_conn("disconnected", "NoDevice: port lost")
        self.assertEqual(sorted(cleared), [0, 1, 2])
        m = self.win.meter
        self.assertEqual((m.big.text(), list(m.vs), m.blank, m.age.text()), ("—", [], False, ""))

    def test_state_updates_to_every_panel_send_nothing(self):
        self.connect(mode=0)
        for p in self.win.panels.values():
            p.set_description(self.desc)
        self.win.panels[0].set_state(state(level=20))
        self.win.panels[1].set_state(object())
        self.win.panels[2].set_state(object())
        self.assertEqual(self.jobs(), [])

    def test_meter_events_grey_the_value_and_go_to_the_events_log(self):
        events = os.path.join(self.tmp, "events.txt")
        w = MainWindow(self.worker, events_path=events,
                       settings=QtCore.QSettings(self.ini, QtCore.QSettings.IniFormat))
        self._wins = [w]
        w.meter.add(self.reading())
        w._worker_event(Event("meter_blank", False, "NOT_READY"))
        self.assertTrue(w.meter.blank)
        self.assertEqual(w.meter.big.styleSheet(), w.meter.BLANK_STYLE)
        w._worker_event(Event("nak", False, "NO_CAPTURE_DATA"))    # other kinds: no change
        self.assertTrue(w.meter.blank)
        w._worker_event(Event("meter_ok", True))
        self.assertFalse(w.meter.blank)
        w._note("meter blank: NOT_READY")
        self.assertIn("no reading yet", w.sb_note.text())
        w._events.close()
        w._events = None
        with open(events, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual([l.split(" ", 2)[2] for l in lines if " event " in l],
                         ["event meter_blank NOT_READY", "event meter_ok"])


METER_NAMES = (("DC Voltage", "V"), ("AC Voltage", "V"), ("DC mA", "mA"), ("DC Current", "A"),
               ("AC mA", "mA"), ("AC Current", "A"), ("Resistance", "kOhm"), ("Continuity", "Ohm"),
               ("Diode", "V"), ("Capacitance", "nF"), ("Temperature", "C"))
USABLE = (0, 1, 6, 7, 8)        # stage-270 bench record (stage-272 section 5)


def meter_reading(uc, sub=0, val=3.254, unit="V", disp=None, flags=0):
    disp = f"{val:.3f}" if disp is None else disp
    return MeterSample(1000.0 + uc, proto.MeterReading(uc, val, 0, 3, 1, flags, sub, 0, unit, disp))


class MeterCase(WindowCase):
    """A stage-272 unit in meter mode: section 5 with five usable functions."""

    def setUp(self):
        super().setUp()
        fns = tuple(proto.MeterFunction(i, i in USABLE, n, u) for i, (n, u) in enumerate(METER_NAMES))
        self.mdesc = dataclasses.replace(self.desc, meter_functions=fns)
        self.p = self.win.panels[1]
        self.uc = 0

    def connect_meter(self, desc="section5"):
        w = self.win
        w._set_conn("connected", "COMFAKE")
        w._description(self.mdesc if desc == "section5" else desc)
        w._status(status(1))
        w._scope_state(state(mode=1))
        self.jobs()

    def read(self, sub=0, **kw):
        self.uc += 1
        self.win._meter(meter_reading(self.uc, sub, **kw))

    def texts(self):
        f = self.p.functions
        return [f.item(i).text() for i in range(f.count())]

    def current(self):
        it = self.p.functions.currentItem()
        return it.data(QtCore.Qt.UserRole) if it is not None and it.isSelected() else None

    def user_click(self, fid):
        """What a mouse click on the item does: it becomes current, then itemClicked."""
        f = self.p.functions
        row = next(i for i in range(f.count()) if f.item(i).data(QtCore.Qt.UserRole) == fid)
        f.setCurrentRow(row)
        f.itemClicked.emit(f.item(row))


class TestMeterPanel(MeterCase):
    def test_the_list_shows_only_usable_functions_by_name(self):
        self.connect_meter()
        self.assertEqual(self.texts(), ["DC Voltage", "AC Voltage", "Resistance", "Continuity", "Diode"])
        f = self.p.functions
        self.assertEqual([f.item(i).data(QtCore.Qt.UserRole) for i in range(f.count())], list(USABLE))
        self.assertIn("Function number 6", f.item(2).toolTip())
        self.assertTrue(f.isEnabled())
        self.assertIs(self.win.stack.currentWidget(), self.p)

    def test_a_real_click_queues_one_set(self):
        self.connect_meter()
        self.read(0)
        f = self.p.functions
        rect = f.visualItemRect(f.item(2))
        QtTest.QTest.mouseClick(f.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, rect.center())
        self.assertEqual(self.jobs(), [("set", ("meter_function", (6,)))])

    def test_a_click_shows_changing_until_a_reading_of_that_function(self):
        self.connect_meter()
        self.read(0)
        self.assertEqual(self.current(), 0)
        self.user_click(6)
        self.assertEqual(self.jobs(), [("set", ("meter_function", (6,)))])
        self.assertEqual(self.texts()[2], "Resistance — changing…")
        self.assertTrue(self.p.timer.isActive())
        self.assertEqual(self.p.timer.interval(), 6000)
        self.read(0)                                    # the old function, before the change
        self.assertEqual((self.current(), self.texts()[2]), (6, "Resistance — changing…"))
        self.user_click(6)                              # a second click on the waiting item
        self.read(6, val=4.7, unit="kOhm")
        self.assertEqual((self.current(), self.texts()[2]), (6, "Resistance"))
        self.assertFalse(self.p.timer.isActive())
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.win.meter.func.text(), "Resistance · kilohms")

    def test_a_unit_function_change_selects_the_item_and_sends_nothing(self):
        self.connect_meter()
        for sub in (0, 6, 7, 8):                        # the Function softkey, 3 changes
            self.read(sub)
            self.assertEqual(self.current(), sub)
            self.assertEqual(self.jobs(), [])
        self.read(3, unit="A")                          # not usable: no item, says so in words
        self.assertIsNone(self.current())
        self.assertEqual(self.p.func_note.text(), "The unit shows DC Current: not in this list")
        self.assertEqual(self.jobs(), [])

    def test_a_refused_change_goes_back_to_the_function_in_force(self):
        self.connect_meter()
        self.read(0)
        self.user_click(6)
        self.jobs()
        self.win._set_done("meter_function", False)
        self.assertEqual((self.current(), self.texts()[2], self.p.pending), (0, "Resistance", None))
        self.user_click(6)                              # the same edit can be sent again
        self.assertEqual(self.jobs(), [("set", ("meter_function", (6,)))])

    def test_no_change_in_6_s_goes_back_and_says_so(self):
        self.connect_meter()
        self.read(0)
        self.user_click(7)
        self.p.timer.timeout.emit()
        self.assertEqual((self.current(), self.p.pending, self.texts()[3]), (0, None, "Continuity"))
        self.assertIn("did not change the function in 6 s", self.p.func_note.text())
        self.read(7)                                    # the change came late: the list follows
        self.assertEqual((self.current(), self.p.func_note.text()), (7, ""))

    def test_no_section_5_disables_the_list_and_readings_still_show(self):
        self.assertEqual(self.desc.meter_functions, ())  # the stage-086 fixture
        for desc in (self.desc, None):                  # no section 5; no description at all
            with self.subTest(desc=desc is not None):
                self.win._set_conn("disconnected", "x")
                self.connect_meter(desc)
                self.read(0)
                self.assertFalse(self.p.functions.isEnabled())
                self.assertEqual(self.p.functions.count(), 0)
                self.assertEqual(self.p.func_note.text(), labels.METER_NO_FUNCTIONS)
                self.assertTrue(self.p.func_note.isVisibleTo(self.win))
                self.assertEqual(self.win.meter.big.text(), "3.254 V")
                self.assertTrue(self.p.hold_btn.isEnabled())

    def test_hold_relative_and_reset_send_nothing(self):
        self.connect_meter()
        self.read(0, val=1.0)
        self.p.hold_btn.click()
        self.read(0, val=2.0)
        self.assertEqual(self.win.meter.big.text(), "1.000 V")
        self.p.hold_btn.click()
        self.p.rel_btn.click()
        self.read(0, val=1.5)
        self.assertEqual(self.win.meter.big.text(), "-0.500 V")
        self.assertEqual((self.p.min_lbl.text(), self.p.max_lbl.text(), self.p.avg_lbl.text(),
                          self.p.count_lbl.text()), ("1.000 V", "2.000 V", "1.5000 V", "3"))
        self.p.reset_btn.click()
        self.assertEqual((self.p.min_lbl.text(), self.p.count_lbl.text()), ("—", "0"))
        self.assertEqual(self.jobs(), [])

    def test_a_function_change_unchecks_hold_and_relative(self):
        self.connect_meter()
        self.read(0)
        self.p.hold_btn.click()
        self.p.rel_btn.click()
        self.read(6, val=4.7, unit="kOhm")
        self.assertEqual((self.p.hold_btn.isChecked(), self.p.rel_btn.isChecked()), (False, False))
        self.assertIn("the meter function changed", self.p.tools_note.text())
        self.assertEqual(self.win.meter.big.text(), "4.700 kΩ")
        self.assertEqual(self.jobs(), [])

    def test_the_blank_note_goes_when_readings_come_back(self):
        self.connect_meter()
        self.win._note("meter blank: NOT_READY")
        self.win._worker_event(Event("meter_ok", True))
        self.assertEqual(self.win.sb_note.text(), "")
        self.win._note("timeout: no reply")                    # another note stays
        self.win._worker_event(Event("meter_ok", True))
        self.assertIn("No reply", self.win.sb_note.text())

    def test_disconnect_clears_the_panel(self):
        self.connect_meter()
        self.read(0)
        self.p.hold_btn.click()
        self.user_click(6)
        self.win._set_conn("disconnected", "NoDevice: port lost")
        p = self.p
        self.assertEqual((p.pending, p.unit_fn, p.hold_btn.isChecked(), p.min_lbl.text()),
                         (None, None, False, "—"))
        self.assertEqual(p.func_note.text(), "Waiting for the unit")
        self.assertFalse(p.functions.isEnabled())
        self.assertFalse(p.hold_btn.isEnabled())
        self.assertFalse(p.timer.isActive())


class TestNoCodesInTheMultimeterTab(MeterCase):
    visible_texts = TestNoCodesVisible.visible_texts

    def test_no_hex_code_or_key_name_with_default_view(self):
        self.connect_meter()
        self.assertEqual(self.win.tabs.currentIndex(), 1)
        self.read(6, val=4.7, unit="kOhm", flags=0x04 | 0x08)     # automatic range, held on the unit
        self.p.hold_btn.click()
        self.p.rel_btn.click()
        self.user_click(7)                              # "changing…"
        self.read(3, unit="A")                          # a function not in the list
        self.win._job_done("set", False, "meter_function refused: BAD_ARG: hint")
        texts = self.visible_texts() + self.texts()
        self.assertTrue(any("Resistance" in t for t in texts))
        self.assertTrue(any("kilohms" in t or "amperes" in t for t in texts))
        self.assertEqual([t for t in texts if HEX.search(t)], [])
        self.assertEqual([t for t in texts if KEY_NAME.search(t)], [])
        words = [re.sub(r"<[^>]+>", "", t) for t in texts]        # no HTML colour codes
        self.assertEqual([t for t in words if re.search(r"submode|function \d|#\d|BAD_ARG", t)], [])


if __name__ == "__main__":
    unittest.main()
