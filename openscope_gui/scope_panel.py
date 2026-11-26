"""Oscilloscope control panel (stage-087): the stage-085 controls in practical
units, probe ×1/×10 per channel and a Single button.

The widgets fill from SCOPE_STATE and the device description without sending
anything: combos act on `activated` (user only), the level on
`editingFinished` (user only), and a state update never sends a set
(stage-085 rule). Every combo item is a practical value or a word; the code
is in the item's tooltip (labels.py).

Single is a trigger mode on the unit: after Single the unit reports
"running" while it waits for, or holds, its one record. So the Run/Stop
button counts trigger Single as not running: it shows "Run", and Run puts
back the trigger mode in force before Single (Auto if none was seen) and
runs. Without that, Run/Stop only re-armed another single capture.

Stage-260: ScopePanel is a panels.Panel; the level box sends through a
panels.EditSender (one set per finished edit) and shows the reported level
only while it has no focus (the Q-1 rule, now in panels.py).
"""
from __future__ import annotations

from typing import List, Optional

from PySide6 import QtCore, QtWidgets

from openscope import proto
from . import labels
from .panels import EditSender, Panel, show_reported, write_value

TIP = QtCore.Qt.ToolTipRole
SINGLE = 2                                      # proto.TRIG_MODES[2]


def continuous(s: proto.ScopeState) -> bool:
    """Running and not in trigger Single (Single holds its record while 'running')."""
    return s.running and s.trig_mode != SINGLE


def fill_combo(cb: QtWidgets.QComboBox, items: List[labels.Item], current: Optional[int]) -> None:
    """Replace the items (only when they changed) and select `current`.
    Sends nothing: `activated` is a user-only signal."""
    want = [(it.value, it.text, it.tooltip) for it in items]
    have = [(cb.itemData(i), cb.itemText(i), cb.itemData(i, TIP)) for i in range(cb.count())]
    if want != have and not cb.view().isVisible():     # never rebuild an open list
        cb.blockSignals(True)
        cb.clear()
        for i, it in enumerate(items):
            cb.addItem(it.text, it.value)
            cb.setItemData(i, it.tooltip, TIP)
        cb.blockSignals(False)
    i = cb.findData(current) if current is not None else -1
    cb.blockSignals(True)
    cb.setCurrentIndex(i)
    cb.blockSignals(False)
    cb.setToolTip(cb.itemData(i, TIP) if i >= 0 else "")


class ScopePanel(Panel):
    mode = 0
    channels_changed = QtCore.Signal(int)       # display mask (bit0 CH1, bit1 CH2)
    level_50 = QtCore.Signal()                  # "Level to 50 %": the window knows the record

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.st: Optional[proto.ScopeState] = None
        self.desc: Optional[proto.Description] = None
        self.show_uncalibrated = False
        self.resume_mode = 0                    # trigger mode that Run restores after Single
        self._lscale = labels.level_scale(None, None)   # scale in force (latest state)
        self._shown: Optional[labels.LevelScale] = None  # scale the level box shows
        self._shown_level: Optional[int] = None          # level last put in the box
        self._shown_value = 0.0                          # box value for it (after rounding)

        v = QtWidgets.QVBoxLayout(self)
        # acquisition
        acq = QtWidgets.QGroupBox("Acquisition")
        h = QtWidgets.QHBoxLayout(acq)
        self.run_btn = QtWidgets.QPushButton("Stop")
        self.run_btn.setToolTip("Run or stop the acquisition (Space)")
        self.run_btn.clicked.connect(self.toggle_run)
        self.single_btn = QtWidgets.QPushButton("Single")
        self.single_btn.setToolTip("Capture one triggered record, then hold it")
        self.single_btn.clicked.connect(self.single)
        h.addWidget(self.run_btn)
        h.addWidget(self.single_btn)
        v.addWidget(acq)

        # display channels (host side only: which channels to read)
        disp = QtWidgets.QGroupBox("Show channels")
        h = QtWidgets.QHBoxLayout(disp)
        self.ch = []
        for i in (1, 2):
            cb = QtWidgets.QCheckBox(f"CH{i}")
            cb.setChecked(i == 1)
            cb.setToolTip(f"Read and draw CH{i} (does not change the unit)")
            cb.toggled.connect(self._channels_toggled)
            h.addWidget(cb)
            self.ch.append(cb)
        v.addWidget(disp)

        # settings that go to the unit
        self.settings = QtWidgets.QGroupBox("Unit settings")
        f = QtWidgets.QFormLayout(self.settings)
        self.tb_cb = QtWidgets.QComboBox()
        self.tb_cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.tb_cb.activated.connect(lambda i: self.worker.set("timebase", self.tb_cb.itemData(i)))
        f.addRow("Time/div", self.tb_cb)
        self.vdiv_cb, self.probe_cb = [], []
        for ch in (1, 2):
            cb = QtWidgets.QComboBox()
            cb.activated.connect(lambda i, ch=ch, cb=cb: self.worker.set("vdiv", ch, cb.itemData(i)))
            pb = QtWidgets.QComboBox()
            fill_combo(pb, labels.probe_items(), None)
            pb.activated.connect(lambda i, ch=ch, pb=pb: self.worker.set("probe", ch, pb.itemData(i)))
            for c in (cb, pb):
                c.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
            f.addRow(f"CH{ch} volts/div", cb)
            f.addRow(f"CH{ch} probe", pb)
            self.vdiv_cb.append(cb)
            self.probe_cb.append(pb)
        self.tmode_cb = QtWidgets.QComboBox()
        self.tedge_cb = QtWidgets.QComboBox()
        self.tsrc_cb = QtWidgets.QComboBox()
        for cb, items in ((self.tmode_cb, labels.word_items(labels.TRIG_MODE_WORDS, "Trigger mode")),
                          (self.tedge_cb, labels.word_items(labels.TRIG_EDGE_WORDS, "Trigger edge")),
                          (self.tsrc_cb, labels.source_items())):
            fill_combo(cb, items, None)
            cb.activated.connect(self._send_trigger)
        self.level_sb = QtWidgets.QDoubleSpinBox()
        self.level_sb.setKeyboardTracking(False)
        self.level_edit = EditSender(self.level_sb, self._send_level, value=self._typed_level,
                                     finished=self._level_finished)
        self._apply_level_scale()
        f.addRow("Trigger", self.tmode_cb)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.tedge_cb, 2)
        row.addWidget(self.tsrc_cb, 1)
        f.addRow("Edge, source", row)
        self.level50_btn = QtWidgets.QPushButton("Level to 50 %")
        self.level50_btn.setToolTip("Put the trigger level in the middle of the trigger "
                                    "source's signal")
        self.level50_btn.clicked.connect(self.level_50.emit)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.level_sb, 2)
        row.addWidget(self.level50_btn, 1)
        f.addRow("Level", row)
        v.addWidget(self.settings)

        self.state_lbl = QtWidgets.QLabel("Waiting for the unit")
        self.state_lbl.setWordWrap(True)
        self.state_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.state_lbl)
        v.addStretch(1)
        self.set_enabled(False)

    # ── called by the window ───────────────────────────────────────
    def set_enabled(self, on: bool) -> None:
        self.settings.setEnabled(on)
        self.run_btn.setEnabled(on)
        self.single_btn.setEnabled(on)

    def set_description(self, d: Optional[proto.Description]) -> None:
        self.desc = d
        self.refresh()

    def set_show_uncalibrated(self, on: bool) -> None:
        self.show_uncalibrated = on
        self.refresh()

    def clear(self) -> None:
        self.st = None
        self.set_enabled(False)
        self.state_lbl.setText("Waiting for the unit")

    def set_state(self, s: proto.ScopeState) -> None:
        self.st = s
        if s.trig_mode != SINGLE:
            self.resume_mode = s.trig_mode
        self.set_enabled(True)
        self.refresh()

    def refresh(self) -> None:
        """Fill every widget from the state and the description. Sends nothing."""
        s, d = self.st, self.desc
        if s is None:
            fill_combo(self.tb_cb, labels.timebase_items(d, self.show_uncalibrated), None)
            for i, cb in enumerate(self.vdiv_cb):
                fill_combo(cb, labels.vdiv_items(d, i + 1, d.probe_factor(i + 1) if d else 1,
                                                 self.show_uncalibrated), None)
            return
        cont = continuous(s)
        self.run_btn.setText("Stop" if cont else "Run")
        self.run_btn.setStyleSheet("" if cont else
                                   "background:#eb5757; color:white; font-weight:bold")
        self.run_btn.setToolTip(
            "Stop the acquisition (Space)" if cont else
            f"Continuous capture, trigger {labels.TRIG_MODE_WORDS[self.resume_mode]} (Space)"
            if s.trig_mode == SINGLE else "Run the acquisition (Space)")
        fill_combo(self.tb_cb, labels.timebase_items(d, self.show_uncalibrated, s.timebase_code),
                   s.timebase_code)
        for i, cb in enumerate(self.vdiv_cb):
            probe = s.probe[i] if s.probe else (d.probe_factor(i + 1) if d else 1)
            r = s.ch[i].vdiv_idx
            fill_combo(cb, labels.vdiv_items(d, i + 1, probe, self.show_uncalibrated, r), r)
            fill_combo(self.probe_cb[i], labels.probe_items(), probe)
            self.probe_cb[i].setEnabled(s.probe is not None)
        fill_combo(self.tmode_cb, labels.word_items(labels.TRIG_MODE_WORDS, "Trigger mode"), s.trig_mode)
        fill_combo(self.tedge_cb, labels.word_items(labels.TRIG_EDGE_WORDS, "Trigger edge"), s.trig_edge)
        fill_combo(self.tsrc_cb, labels.source_items(), s.trig_source)
        self._lscale = labels.level_scale(d, s)
        self.level_edit.reported(s.trig_level)
        self._show_level(s.trig_level)          # not while a level is being typed
        self.level_sb.setToolTip(labels.level_tooltip(s))
        self.state_lbl.setText("\n".join(labels.state_lines(s, d)))

    # ── user actions ───────────────────────────────────────────────
    def toggle_run(self) -> None:
        s = self.st
        if s is None:
            return
        if s.trig_mode == SINGLE:               # back to continuous capture
            self.worker.set("trigger", proto.TRIG_MODES[self.resume_mode], proto.TRIG_EDGES[s.trig_edge],
                            proto.TRIG_SOURCES[s.trig_source], s.trig_level)
            self.worker.set("run", True)
        else:
            self.worker.set("run", not s.running)

    def single(self) -> None:
        """Trigger mode Single, then run: the unit takes one record and holds it."""
        if self.st is None:
            return
        s = self.st
        self.worker.set("trigger", proto.TRIG_MODES[2], proto.TRIG_EDGES[s.trig_edge],
                        proto.TRIG_SOURCES[s.trig_source], s.trig_level)
        self.worker.set("run", True)

    def _channels_toggled(self, *_):
        mask = sum(1 << i for i, cb in enumerate(self.ch) if cb.isChecked())
        if not mask:                            # at least one channel
            self.ch[0].setChecked(True)
            return
        self.channels_changed.emit(mask)

    def set_channels(self, mask: int) -> None:
        for i, cb in enumerate(self.ch):
            cb.blockSignals(True)
            cb.setChecked(bool(mask & (1 << i)))
            cb.blockSignals(False)

    def set_level_counts(self, counts: float) -> None:
        """Set the trigger level so the unit fires at `counts` (plot drag, Level to 50 %)."""
        lv = labels.level_for_counts(self.desc, counts)
        if lv is None or self.st is None:
            return
        self._show_level(lv, force=True)
        if lv != self.st.trig_level:
            self._send_trigger(level=lv)
            self.level_edit.last = lv           # a focus out of the box does not send it again

    def level(self) -> int:
        """The level for the box's value. A value the user did not edit is the level
        last shown (never read again on a newer scale). Volts typed under an older
        range are read on the range in force (volts stay volts); a unit change
        (V <-> %) uses the scale the box shows."""
        if self._shown_level is not None and self.level_sb.value() == self._shown_value:
            return self._shown_level
        ls = self._lscale if self._shown is None or self._lscale.unit == self._shown.unit else self._shown
        return ls.level(self.level_sb.value())

    def _send_trigger(self, *_, level: Optional[int] = None):
        self.worker.set("trigger", proto.TRIG_MODES[self.tmode_cb.currentData()],
                        proto.TRIG_EDGES[self.tedge_cb.currentData()],
                        proto.TRIG_SOURCES[self.tsrc_cb.currentData()],
                        self.level() if level is None else level)

    def _typed_level(self) -> Optional[int]:
        """The level the box asks for (None: no state yet, nothing to send)."""
        return None if self.st is None else self.level()

    def _send_level(self, lv: int) -> None:
        """EditSender: Enter or focus out with a new level."""
        self._send_trigger(level=lv)

    def _level_finished(self, lv: Optional[int]) -> None:
        """After every finished edit: show the level on the scale in force."""
        if lv is not None:
            self._show_level(lv, force=True)

    def _show_level(self, lv: int, force: bool = False) -> None:
        """Put the scale in force and level `lv` in the box. Sends nothing.
        Only when the box has no focus, unless `force` (the user's own edit)."""
        sb = self.level_sb
        if sb.hasFocus() and not force:
            return
        self._apply_level_scale()
        (write_value if force else show_reported)(sb, self._lscale.value(lv))
        self._shown_level, self._shown_value = lv, sb.value()

    def _apply_level_scale(self) -> None:
        """Set the box to the scale in force, only when it changed: each setter makes
        Qt write the edit text again (code review 2026-11-23 Q-1)."""
        ls = self._lscale
        if ls == self._shown:
            return
        self._shown = ls
        sb = self.level_sb
        sb.blockSignals(True)
        sb.setDecimals(ls.decimals)
        sb.setSingleStep(ls.step)
        sb.setRange(ls.value(ls.level_min), ls.value(ls.level_max))
        sb.setSuffix(" V" if ls.unit == "V" else " % of screen")
        sb.blockSignals(False)
