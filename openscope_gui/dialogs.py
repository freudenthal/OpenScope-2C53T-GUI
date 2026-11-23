"""Dialogs (stage-087): device information, debug console, about."""
from __future__ import annotations

import html
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from . import labels
from .worker import shell_refused


class DeviceInfoDialog(QtWidgets.QDialog):
    """The `openscope info` data, in words. Fills when the worker answers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device information")
        self.form = QtWidgets.QFormLayout()
        self.wait = QtWidgets.QLabel("Asking the unit…")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.wait)
        lay.addLayout(self.form)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(420, 300)

    def show_info(self, info: dict) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.wait.hide()
        for label, value in labels.info_lines(info["version"], info["port"], info["status"],
                                              info.get("state"), info.get("description")):
            v = QtWidgets.QLabel(value)
            v.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.form.addRow(label, v)

    def failed(self, why: str) -> None:
        self.wait.setText(f"No answer from the unit: {labels.note_words(why)}")


class DebugConsoleDialog(QtWidgets.QDialog):
    """One debug-shell line at a time, through the worker (the only port user).
    `cal restore` and `mem write` are refused here (I-40)."""

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.setWindowTitle("Debug console")
        warn = QtWidgets.QLabel("Commands go to the unit's debug shell as typed. Some can erase "
                                "flash or stop the capture. Calibration restore and memory write "
                                "are refused.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#eb5757; font-weight:bold")
        self.out = QtWidgets.QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.line = QtWidgets.QLineEdit()
        self.line.setPlaceholderText("command, then Enter (for example: version)")
        self.line.returnPressed.connect(self.send)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(warn)
        lay.addWidget(self.out, 1)
        lay.addWidget(self.line)
        worker.shell_out.connect(self.show_output)
        self.resize(640, 420)

    def send(self) -> None:
        cmd = self.line.text().strip()
        if not cmd:
            return
        self.line.clear()
        if shell_refused(cmd):
            self.out.appendPlainText(f"> {cmd}\nrefused: this command is never sent to the unit")
            return
        self.out.appendPlainText(f"> {cmd}")
        self.worker.shell(cmd)

    def show_output(self, cmd: str, text: str) -> None:
        self.out.appendPlainText(text.rstrip())


def about_text(firmware: str) -> str:
    from openscope import __version__ as lib
    from . import __version__ as gui
    try:
        import PySide6
        qt = PySide6.__version__
    except Exception:                       # pragma: no cover
        qt = "?"
    try:
        import pyqtgraph
        pg = pyqtgraph.__version__
    except Exception:                       # pragma: no cover
        pg = "?"
    py = ".".join(str(x) for x in sys.version_info[:3])
    rows = [("OpenScope GUI", gui), ("openscope library", lib),
            ("Firmware", firmware or "not connected"),
            ("PySide6", qt), ("pyqtgraph", pg), ("Python", py)]
    body = "".join(f"<tr><td>{html.escape(a)}</td><td>&nbsp;{html.escape(b)}</td></tr>" for a, b in rows)
    return ("<b>OpenScope 2C53T</b><p>Live oscilloscope, multimeter and control of the FNIRSI "
            f"2C53T running OpenScope firmware, over USB.</p><table>{body}</table>")


def show_about(parent, firmware: str) -> None:
    QtWidgets.QMessageBox.about(parent, "About OpenScope", about_text(firmware))
