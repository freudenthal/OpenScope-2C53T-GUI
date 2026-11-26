"""openscope_gui/panels.py (stage-260): the Panel interface, EditSender and
show_reported, offscreen. Needs the `gui` extra; skipped without it. Only a
missing PySide6 skips; an import error inside openscope_gui fails the suite."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)

HAVE_QT = all(importlib.util.find_spec(m) is not None for m in ("PySide6", "numpy", "pyqtgraph"))
if HAVE_QT:
    from PySide6 import QtCore, QtTest, QtWidgets
    from openscope_gui import panels  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestEditSender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.top = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(self.top)
        self.sb = QtWidgets.QDoubleSpinBox()
        self.sb.setRange(-100, 100)
        self.sb.setDecimals(2)
        self.sb.setKeyboardTracking(False)
        self.other = QtWidgets.QLineEdit()
        self.cb = QtWidgets.QComboBox()
        for i, name in enumerate(("a", "b", "c")):
            self.cb.addItem(name, i * 10)
        for w in (self.sb, self.other, self.cb):
            lay.addWidget(w)
        self.top.show()
        self.sent = []
        self.es = panels.EditSender(self.sb, self.sent.append)

    def tearDown(self):
        self.top.hide()
        self.top.deleteLater()

    def focus(self, w):
        self.top.activateWindow()
        w.setFocus()
        self.app.processEvents()
        self.assertTrue(w.hasFocus())

    def test_focus_keeps_the_typed_text(self):
        self.es.show_reported(1.0)
        self.focus(self.sb)
        self.sb.selectAll()
        QtTest.QTest.keyClicks(self.sb, "42.50")
        self.assertFalse(self.es.show_reported(3.0))        # a state update while typing
        self.assertFalse(panels.show_reported(self.sb, 7.0))
        self.assertEqual(self.sb.cleanText(), "42.50")
        self.assertEqual(self.sent, [])
        QtTest.QTest.keyClick(self.sb, QtCore.Qt.Key_Return)
        self.assertEqual(self.sent, [42.5])

    def test_editing_finished_sends_once(self):
        self.focus(self.sb)
        self.sb.selectAll()
        QtTest.QTest.keyClicks(self.sb, "12.25")
        QtTest.QTest.keyClick(self.sb, QtCore.Qt.Key_Return)   # Enter ...
        self.focus(self.other)                                  # ... then focus out
        self.sb.editingFinished.emit()
        self.assertEqual(self.sent, [12.25])
        self.assertEqual(self.es.sent, 1)

    def test_reported_value_is_not_sent_and_a_refusal_can_be_sent_again(self):
        self.es.show_reported(5.0)
        self.sb.editingFinished.emit()                          # focus out, no edit
        self.assertEqual(self.sent, [])
        self.sb.setValue(9.0)
        self.sb.editingFinished.emit()
        self.assertEqual(self.sent, [9.0])
        self.es.reported(5.0)                                   # the unit refused: still 5
        self.sb.editingFinished.emit()
        self.assertEqual(self.sent, [9.0, 9.0])

    def test_show_reported_blocks_signals_and_writes_only_a_change(self):
        changed = []
        self.sb.valueChanged.connect(changed.append)
        self.assertTrue(panels.show_reported(self.sb, 3.0))
        self.assertFalse(panels.show_reported(self.sb, 3.0))   # same value: no write
        self.assertFalse(panels.show_reported(self.sb, 3.001))  # same after rounding to 2 decimals
        self.assertEqual(self.sb.value(), 3.0)
        self.assertEqual(changed, [])
        self.assertEqual(self.sent, [])

    def test_combo_sends_on_activated_only(self):
        got = []
        panels.EditSender(self.cb, got.append)
        panels.show_reported(self.cb, 20)                      # a state update
        self.assertEqual(self.cb.currentText(), "c")
        self.assertEqual(got, [])
        self.cb.setCurrentIndex(1)
        self.cb.activated.emit(1)                               # the user's choice
        self.assertEqual(got, [10])

    def test_value_none_is_never_sent_but_finished_runs(self):
        done, got = [], []
        panels.EditSender(self.other, got.append, value=lambda: None, finished=done.append)
        self.other.setText("x")
        self.other.editingFinished.emit()
        self.assertEqual((got, done), ([], [None]))


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestPanels(unittest.TestCase):
    setUpClass = TestEditSender.setUpClass

    def test_placeholder_panel(self):
        p = panels.PlaceholderPanel(1, "Multimeter function controls come in stage 275.")
        self.assertIsInstance(p, panels.Panel)
        self.assertEqual((p.mode, p.text()), (1, "Multimeter function controls come in stage 275."))
        p.set_description(None)
        p.set_state(object())
        p.clear()
        self.assertTrue(p.label.isEnabled())                    # the text stays readable

    def test_panel_interface(self):
        p = panels.Panel()
        for name in ("set_description", "set_state", "clear", "set_enabled"):
            self.assertTrue(callable(getattr(p, name)), name)
        p.clear()
        self.assertFalse(p.isEnabled())
        p.set_enabled(True)
        self.assertTrue(p.isEnabled())


if __name__ == "__main__":
    unittest.main()
