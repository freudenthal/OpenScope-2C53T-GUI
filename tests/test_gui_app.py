"""gui/app.py scope settings panel (stage-085), offscreen: the window with a
real Worker that is never started, so queued jobs can be inspected and no
port is touched. Needs the `gui` extra; skipped without it."""
from __future__ import annotations

import os
import queue
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openscope import proto  # noqa: E402

try:
    import numpy as np  # noqa: F401
    from PySide6 import QtWidgets
    from openscope.gui.app import PROBE_NOTE, MainWindow  # noqa: E402
    from openscope.gui.worker import ScopeFrame, Worker  # noqa: E402
    HAVE_QT = True
except ImportError:          # pragma: no cover - depends on the venv
    HAVE_QT = False


def state(running=True, tb=0x10, mode=0, r1=6, r2=7, tmode=0, edge=0, src=0, level=0):
    ch = (proto.ScopeChannel(True, r1, 2, 42950), proto.ScopeChannel(True, r2, 2, 88420))
    return proto.ScopeState(1, mode, running, tb, tb, 2, tmode, edge, src, level, 12490, 0x80, 32, ch)


def frame(tb=0x10, r1=6, fid=7):
    w = proto.Waveform(fid, 0, proto.WAVE_FLAG_TB_MEASURED, tb, r1, 1024, 24, 12490, 42950, 32, 24,
                       bytes(1024))
    return ScopeFrame.from_waves((w,), 0.0)


def no_device():
    raise RuntimeError("tests never open a port")


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestSettingsPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.worker = Worker(no_device)
        self.win = MainWindow(self.worker)

    def tearDown(self):
        self.win.timer.stop()
        self.win.rate_timer.stop()
        self.win.deleteLater()

    def jobs(self):
        out = []
        while True:
            try:
                out.append(self.worker._jobs.get_nowait())
            except queue.Empty:
                return out

    def test_disabled_until_a_state_arrives(self):
        self.assertFalse(self.win.settings.isEnabled())
        self.win._scope_state(state())
        self.assertTrue(self.win.settings.isEnabled())
        self.win._set_conn("searching", "")
        self.assertFalse(self.win.settings.isEnabled())

    def test_a_state_fills_the_widgets_and_sends_nothing(self):
        self.win._scope_state(state(running=False, tb=0x12, mode=0, r1=5, r2=8, tmode=2, edge=1,
                                    src=1, level=-40))
        w = self.win
        self.assertEqual((w.tb_cb.currentIndex(), w.vdiv_cb[0].currentIndex(), w.vdiv_cb[1].currentIndex()),
                         (0x12, 5, 8))
        self.assertEqual((w.tmode_cb.currentText(), w.tedge_cb.currentText(), w.tsrc_cb.currentText(),
                          w.level_sb.value()), ("single", "falling", "CH2", -40))
        self.assertEqual(w.run_btn.text(), "Run")
        self.assertIn("STOPPED", w.state_lbl.text())
        self.assertEqual(self.jobs(), [])

    def test_each_user_change_queues_exactly_one_set(self):
        w = self.win
        w._scope_state(state())
        w.tb_cb.activated.emit(0x14)
        w.vdiv_cb[1].activated.emit(3)
        w.mode_cb.activated.emit(1)
        w.run_btn.click()
        w.tmode_cb.setCurrentIndex(1)
        w.tmode_cb.activated.emit(1)
        self.assertEqual(self.jobs(), [("set", ("timebase", (0x14,))), ("set", ("vdiv", (2, 3))),
                                       ("set", ("mode", ("meter",))), ("set", ("run", (False,))),
                                       ("set", ("trigger", ("normal", "rising", "CH1", 0)))])

    def test_level_is_sent_only_when_it_changed(self):
        w = self.win
        w._scope_state(state(level=10))
        w.level_sb.editingFinished.emit()           # same value: nothing
        self.assertEqual(self.jobs(), [])
        w.level_sb.setValue(-25)
        w.level_sb.editingFinished.emit()
        self.assertEqual(self.jobs(), [("set", ("trigger", ("auto", "rising", "CH1", -25)))])

    def test_held_record_and_stopped_notes(self):
        w = self.win
        f = frame(tb=0x10, r1=6)
        w._scope_state(state(running=True, tb=0x10, r1=6))
        self.assertEqual(w.frame_notes(f), [])
        w._scope_state(state(running=False, tb=0x12, r1=7))
        notes = w.frame_notes(f)
        self.assertEqual(notes[0], "STOPPED (held record)")
        self.assertIn("timebase 0x10 (now 0x12)", notes[1])
        self.assertIn("CH1 range 6 (now 7)", notes[1])
        w._frame(f)
        self.assertIn("record taken at", w.sb_flags.text())

    def test_a_state_update_refreshes_the_notes_of_a_held_frame(self):
        w = self.win
        w._scope_state(state())
        w._frame(frame())
        self.assertNotIn("STOPPED", w.sb_flags.text())
        w._scope_state(state(running=False))            # no new frame comes under STOP
        self.assertIn("STOPPED", w.sb_flags.text())

    def test_probe_note_is_shown(self):
        texts = [lbl.text() for lbl in self.win.settings.findChildren(QtWidgets.QLabel)]
        self.assertIn(PROBE_NOTE, texts)

    def test_screenshot_holds_by_default(self):
        self.assertTrue(self.win.hold_shot.isChecked())


if __name__ == "__main__":
    unittest.main()
