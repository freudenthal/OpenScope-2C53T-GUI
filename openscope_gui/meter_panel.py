"""Multimeter control panel (stage-275).

* Function list: the usable functions of this firmware by name (description
  section 5, stage-272). A user click on an item sends one
  `set("meter_function", id)` (panels.EditSender). The item shows
  "changing…" until a reading of that function comes (the unit ACKs when the
  change is queued; the bench took 2.4-3.1 s), the set is refused, or
  CHANGE_TIMEOUT_MS passes; then the list shows the function in force.
* A reading of another function (the unit's Function softkey) selects that
  item with signals blocked: a reading never sends a set (stage-087 rule).
* Hold, Relative and minimum/maximum/average are host tools on the
  MeterModel of the Multimeter tab: they never send a command, and the CSV
  log keeps the readings as measured.
* Firmware without section 5 (API level 1): the list is disabled with one
  line of text; the readings still show.

Codes (function numbers) are in tooltips only.
"""
from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

from . import labels
from .panels import EditSender, Panel, list_row
from .views import MeterView, display_decimals

MODE_METER = 1
CHANGE_TIMEOUT_MS = 6000        # stage-272 bench: in force within 3.12 s; plan limit 6 s
WAITING = "Waiting for the unit"


class MeterPanel(Panel):
    mode = MODE_METER

    def __init__(self, worker, view: MeterView, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.view = view
        self.model = view.model
        self.desc = None
        self.unit_fn: Optional[int] = None      # submode of the last reading
        self.pending: Optional[int] = None      # function asked for, not yet in force
        self._waiting = True                    # no description or reading since the last clear
        self.note_text = ""

        lay = QtWidgets.QVBoxLayout(self)
        box = QtWidgets.QGroupBox("Function")
        bl = QtWidgets.QVBoxLayout(box)
        self.functions = QtWidgets.QListWidget()
        self.functions.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.functions.setToolTip("Click a function to select it on the unit")
        self.func_note = QtWidgets.QLabel(WAITING)
        self.func_note.setWordWrap(True)
        bl.addWidget(self.functions)
        bl.addWidget(self.func_note)
        lay.addWidget(box)
        self.sender = EditSender(self.functions, self._send)

        box = QtWidgets.QGroupBox("Display")
        bl = QtWidgets.QHBoxLayout(box)
        self.hold_btn = QtWidgets.QPushButton("Hold")
        self.hold_btn.setCheckable(True)
        self.hold_btn.setToolTip(labels.METER_HOLD_TIP)
        self.hold_btn.toggled.connect(self._hold)
        self.rel_btn = QtWidgets.QPushButton("Relative (zero)")
        self.rel_btn.setCheckable(True)
        self.rel_btn.setToolTip(labels.METER_REL_TIP)
        self.rel_btn.toggled.connect(self._relative)
        bl.addWidget(self.hold_btn)
        bl.addWidget(self.rel_btn)
        self.display_box = box
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Minimum, maximum, average")
        fl = QtWidgets.QFormLayout(box)
        self.min_lbl, self.max_lbl, self.avg_lbl, self.count_lbl = (QtWidgets.QLabel("—") for _ in range(4))
        for name, w in (("Minimum", self.min_lbl), ("Maximum", self.max_lbl),
                        ("Average", self.avg_lbl), ("Readings", self.count_lbl)):
            fl.addRow(name, w)
        self.reset_btn = QtWidgets.QPushButton("Reset min/max")
        self.reset_btn.setToolTip(labels.METER_RESET_TIP)
        self.reset_btn.clicked.connect(self.reset_stats)
        fl.addRow(self.reset_btn)
        self.stats_box = box
        lay.addWidget(box)

        self.tools_note = QtWidgets.QLabel("")
        self.tools_note.setWordWrap(True)
        lay.addWidget(self.tools_note)
        lay.addStretch(1)

        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timed_out)
        self.set_enabled(False)

    # ── Panel interface ───────────────────────────────────────────
    def set_description(self, d) -> None:
        self.desc = d
        self.model.set_names(d)
        self._end_pending()
        self.functions.clear()
        fns = [f for f in (getattr(d, "meter_functions", ()) if d is not None else ()) if f.usable]
        for f in fns:
            it = QtWidgets.QListWidgetItem(f.name)
            it.setData(QtCore.Qt.UserRole, f.id)
            it.setToolTip(labels.function_tooltip(f))
            self.functions.addItem(it)
        if d is not None:
            self._waiting = False
        self.functions.setEnabled(bool(fns))
        self.sender.reported(None)
        if self.unit_fn is not None:
            self._select(self.unit_fn)
        self._show_note()

    def set_state(self, s) -> None:
        """A reading (worker.MeterSample, or a proto.MeterReading). Selects
        the function in force; sends nothing."""
        r = getattr(s, "reading", s)
        submode = getattr(r, "submode", None)
        if submode is None:
            return
        self._waiting = False
        self.set_enabled(True)
        if self.pending is not None:
            if submode == self.pending:
                self._end_pending()
            elif submode == self.unit_fn:
                self._show_tools()              # the old function until the change is in force
                return
            else:                               # a third function: the unit's own key
                self._end_pending()
        if submode != self.unit_fn:
            self.note_text = ""
        self.unit_fn = submode
        self._select(submode)
        self._show_note()
        self._show_tools()

    def clear(self) -> None:
        self._end_pending()
        self.model.end_tools("")                # no Hold or Relative from the last connection
        self.unit_fn = None
        self._waiting = True
        self.sender.reported(None)
        self.functions.blockSignals(True)
        self.functions.clearSelection()
        self.functions.blockSignals(False)
        self._sync_buttons()
        self._show_stats()
        self.tools_note.setText("")
        self.set_enabled(False)
        self._show_note()

    def set_enabled(self, on: bool) -> None:
        for w in (self.display_box, self.stats_box):
            w.setEnabled(on)

    # ── the function list ─────────────────────────────────────────
    def _send(self, fid) -> None:
        self.pending = fid
        self.note_text = ""
        self._show_items()
        self.timer.start(CHANGE_TIMEOUT_MS)
        self.worker.set("meter_function", fid)

    def set_done(self, what: str, ok: bool) -> None:
        """The worker's set_done: a refused function change ends the wait."""
        if what == "meter_function" and not ok and self.pending is not None:
            self._end_pending()
            self._restore()

    def _timed_out(self) -> None:
        self._end_pending()
        self._restore()
        self.note_text = "The unit did not change the function in 6 s"
        self._show_note()

    def _restore(self) -> None:
        """Show the function in force again (after a refusal or a timeout)."""
        self.sender.reported(self.unit_fn)
        self._select(self.unit_fn)

    def _end_pending(self) -> None:
        self.timer.stop()
        if self.pending is not None:
            self.pending = None
            self._show_items()
        self.note_text = ""

    def _select(self, fid: Optional[int]) -> None:
        self.sender.reported(fid)
        if list_row(self.functions, fid) < 0:
            self.functions.blockSignals(True)
            self.functions.clearSelection()
            self.functions.setCurrentRow(-1)
            self.functions.blockSignals(False)
        else:
            self.sender.show_reported(fid)

    def _show_items(self) -> None:
        for i in range(self.functions.count()):
            it = self.functions.item(i)
            fid = it.data(QtCore.Qt.UserRole)
            name = self.model.names.get(fid, it.text())
            it.setText(f"{name} — {labels.METER_CHANGING}" if fid == self.pending else name)

    def _show_note(self) -> None:
        if self._waiting:
            text = WAITING
        elif not self.functions.count():
            text = labels.METER_NO_FUNCTIONS
        elif self.note_text:
            text = self.note_text
        elif self.unit_fn is not None and list_row(self.functions, self.unit_fn) < 0:
            name = self.model.names.get(self.unit_fn, "another function")
            text = f"The unit shows {name}: {labels.METER_NOT_IN_LIST}"
        else:
            text = ""
        self.func_note.setText(text)
        self.func_note.setVisible(bool(text))

    # ── host tools ────────────────────────────────────────────────
    def _hold(self, on: bool) -> None:
        self.model.set_hold(on)
        self._after_tool()

    def _relative(self, on: bool) -> None:
        self.model.set_relative(on)
        self._after_tool()

    def reset_stats(self) -> None:
        self.model.reset_stats()
        self._after_tool()

    def _after_tool(self) -> None:
        self.tools_note.setText("")
        self._sync_buttons()
        self._show_stats()
        self.view.show_value()

    def _show_tools(self) -> None:
        """After a reading: the buttons and the statistics from the model."""
        self._sync_buttons()
        self._show_stats()
        if self.model.tools_note:
            self.tools_note.setText(f"Hold, Relative and min/max ended: {self.model.tools_note}")

    def _sync_buttons(self) -> None:
        for btn, on in ((self.hold_btn, self.model.hold), (self.rel_btn, self.model.relative)):
            if btn.isChecked() != on:
                btn.blockSignals(True)
                btn.setChecked(on)
                btn.blockSignals(False)

    def _show_stats(self) -> None:
        st = self.model.stats()
        if st is None:
            for w in (self.min_lbl, self.max_lbl, self.avg_lbl):
                w.setText("—")
            self.count_lbl.setText("0")
            return
        unit = self.model.unit
        dec = display_decimals(self.model.last.reading.display) if self.model.last is not None else 3
        lo, hi, avg, n = st
        self.min_lbl.setText(labels.fmt_meter(lo, dec, unit))
        self.max_lbl.setText(labels.fmt_meter(hi, dec, unit))
        self.avg_lbl.setText(labels.fmt_meter(avg, dec + 1, unit))
        self.count_lbl.setText(str(n))
