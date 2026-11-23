"""`openscope-gui`: live scope and meter view of the unit over USB CDC.

Stage-085 adds the scope settings (run/stop, timebase, volts/div, trigger,
mode) through the 080 control surface; the window shows the state the unit
reports back, never the value it asked for. One reader thread owns the port
(worker.py); a 30 Hz timer draws the latest data; the status bar shows
connection, mode, battery, link frames/s, redraws/s and the frame's trust flags.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import proto
from ..cli import _state_lines
from .views import MeterView, Rate, ScopeView
from .worker import ScopeFrame, Worker

DRAW_HZ = 30
KEYS = [["CH1", "CH2", "MOVE", "SELECT", "TRIGGER"],
        ["PRM", "AUTO", "SAVE", "MENU", "OK"],
        ["UP", "DOWN", "LEFT", "RIGHT"]]          # POWER left out on purpose (shuts the unit down)
PROBE_NOTE = ("Volts include the unit's probe setting (x1/x10), which is not sent over USB: "
              "check it on the unit (C-100, G-280).")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, worker: Worker, stats_path: Optional[str] = None,
                 events_path: Optional[str] = None):
        super().__init__()
        self.worker = worker
        self.setWindowTitle("OpenScope 2C53T")
        self.resize(1100, 720)
        self.scope = ScopeView()
        self.meter = MeterView()
        self.views = QtWidgets.QTabWidget()
        self.views.addTab(self.scope, "Scope")
        self.views.addTab(self.meter, "Meter")
        self.setCentralWidget(self.views)
        self._follow_mode = True
        self._last_mode = None
        self.ticks = Rate()
        self._events = None

        # controls dock
        side = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(side)
        box = QtWidgets.QGroupBox("Channels")
        hb = QtWidgets.QHBoxLayout(box)
        self.ch = []
        for i in (1, 2):
            cb = QtWidgets.QCheckBox(f"CH{i}")
            cb.setChecked(i == 1)
            cb.toggled.connect(self._channels_changed)
            hb.addWidget(cb)
            self.ch.append(cb)
        v.addWidget(box)
        self.pause = QtWidgets.QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self._pause)
        v.addWidget(self.pause)
        self.shot = QtWidgets.QPushButton("Screenshot…")
        self.shot.clicked.connect(self._screenshot)
        v.addWidget(self.shot)
        self.hold_shot = QtWidgets.QCheckBox("Hold scope for screenshot")
        self.hold_shot.setToolTip("STOP for the dump, then RUN again: a live screen is torn (G-260)")
        self.hold_shot.setChecked(True)
        v.addWidget(self.hold_shot)
        self.save = QtWidgets.QPushButton("Save frame (.npz)…")
        self.save.clicked.connect(self._save_npz)
        v.addWidget(self.save)
        self.follow = QtWidgets.QCheckBox("Follow unit mode")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda on: setattr(self, "_follow_mode", on))
        v.addWidget(self.follow)
        v.addWidget(self._settings_box())
        keys = QtWidgets.QGroupBox("Unit buttons")
        g = QtWidgets.QGridLayout(keys)
        for r, row in enumerate(KEYS):
            for c, name in enumerate(row):
                b = QtWidgets.QPushButton(name)
                b.clicked.connect(lambda _=False, n=name: self.worker.press(n))
                g.addWidget(b, r, c)
        v.addWidget(keys)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.info)
        v.addStretch(1)
        dock = QtWidgets.QDockWidget("Control", self)
        dock.setWidget(side)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)

        # status bar
        self.sb_conn = QtWidgets.QLabel()
        self.sb_mode = QtWidgets.QLabel()
        self.sb_rate = QtWidgets.QLabel()
        self.sb_flags = QtWidgets.QLabel()
        self.sb_note = QtWidgets.QLabel()
        for w in (self.sb_conn, self.sb_mode, self.sb_rate, self.sb_flags):
            self.statusBar().addWidget(w)
        self.statusBar().addPermanentWidget(self.sb_note)
        self._set_conn("searching", "")

        # wiring
        worker.connection.connect(self._set_conn)
        worker.status.connect(self._status)
        worker.frame.connect(self._frame)
        worker.meter.connect(self.meter.add)
        worker.note.connect(self._note)
        worker.job_done.connect(self._job_done)
        worker.scope_state.connect(self._scope_state)
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / DRAW_HZ))
        self.rate_timer = QtCore.QTimer(self)
        self.rate_timer.timeout.connect(self._rates)
        self.rate_timer.start(1000)
        self._stats = None
        if stats_path:
            self._stats_f = open(stats_path, "a", newline="")
            self._stats = csv.writer(self._stats_f)
            if self._stats_f.tell() == 0:
                self._stats.writerow(["t_unix", "state", "mode", "channels", "link_fps",
                                      "draws_per_s", "timer_hz", "frames", "meter", "naks",
                                      "connects", "losses", "frame_id",
                                      "ch1_fft_hz", "ch1_period_hz", "ch2_fft_hz", "ch2_period_hz"])
        self._events = open(events_path, "a", encoding="utf-8") if events_path else None
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+G"), self, activated=self._grab)
        self._state = "searching"

    # ── scope settings (stage-085) ─────────────────────────────────
    def _settings_box(self) -> QtWidgets.QGroupBox:
        """Widgets fill from SCOPE_STATE without sending anything: combos act on
        `activated` (user only), the level on `editingFinished` (user only)."""
        self.st: Optional[proto.ScopeState] = None
        box = QtWidgets.QGroupBox("Scope settings")
        f = QtWidgets.QFormLayout(box)
        self.run_btn = QtWidgets.QPushButton("Stop")
        self.run_btn.clicked.connect(self._toggle_run)
        f.addRow("Run/stop", self.run_btn)
        self.mode_cb = QtWidgets.QComboBox()
        self.mode_cb.addItems([proto.MODES[i] for i in sorted(proto.MODES)])
        self.mode_cb.activated.connect(lambda i: self.worker.set("mode", proto.MODES[i]))
        f.addRow("Mode", self.mode_cb)
        self.tb_cb = QtWidgets.QComboBox()
        self.tb_cb.addItems([f"0x{c:02X}" for c in range(proto.TIMEBASE_CODES)])
        self.tb_cb.activated.connect(lambda i: self.worker.set("timebase", i))
        f.addRow("Timebase", self.tb_cb)
        self.vdiv_cb = []
        for ch in (1, 2):
            cb = QtWidgets.QComboBox()
            cb.addItems([f"range {r}" for r in range(proto.VDIV_RANGES)])
            cb.activated.connect(lambda r, ch=ch: self.worker.set("vdiv", ch, r))
            f.addRow(f"CH{ch} volts/div", cb)
            self.vdiv_cb.append(cb)
        self.tmode_cb = QtWidgets.QComboBox()
        self.tmode_cb.addItems([proto.TRIG_MODES[i] for i in sorted(proto.TRIG_MODES)])
        self.tedge_cb = QtWidgets.QComboBox()
        self.tedge_cb.addItems([proto.TRIG_EDGES[i] for i in sorted(proto.TRIG_EDGES)])
        self.tsrc_cb = QtWidgets.QComboBox()
        self.tsrc_cb.addItems([proto.TRIG_SOURCES[i] for i in sorted(proto.TRIG_SOURCES)])
        for cb in (self.tmode_cb, self.tedge_cb, self.tsrc_cb):
            cb.activated.connect(self._send_trigger)
        self.level_sb = QtWidgets.QSpinBox()
        self.level_sb.setRange(-proto.TRIG_LEVEL_MAX, proto.TRIG_LEVEL_MAX)
        self.level_sb.setKeyboardTracking(False)
        self.level_sb.editingFinished.connect(self._level_done)
        trig = QtWidgets.QHBoxLayout()
        for w in (self.tmode_cb, self.tedge_cb, self.tsrc_cb, self.level_sb):
            trig.addWidget(w)
        f.addRow("Trigger", trig)
        self.state_lbl = QtWidgets.QLabel("no scope state yet")
        self.state_lbl.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.state_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.addRow(self.state_lbl)
        probe = QtWidgets.QLabel(PROBE_NOTE)
        probe.setWordWrap(True)
        f.addRow(probe)
        self.settings = box
        box.setEnabled(False)
        return box

    def _toggle_run(self) -> None:
        if self.st is not None:
            self.worker.set("run", not self.st.running)

    def _send_trigger(self, *_):
        self.worker.set("trigger", self.tmode_cb.currentText(), self.tedge_cb.currentText(),
                        self.tsrc_cb.currentText(), self.level_sb.value())

    def _level_done(self) -> None:
        if self.st is None or self.level_sb.value() != self.st.trig_level:
            self._send_trigger()

    def _scope_state(self, s: proto.ScopeState) -> None:
        self.st = s
        self.settings.setEnabled(True)
        self.run_btn.setText("Stop" if s.running else "Run")
        self.run_btn.setStyleSheet("" if s.running else "background:#eb5757; color:white; font-weight:bold")
        self.mode_cb.setCurrentIndex(s.mode)
        self.tb_cb.setCurrentIndex(s.timebase_code)
        for cb, c in zip(self.vdiv_cb, s.ch):
            cb.setCurrentIndex(c.vdiv_idx)
        self.tmode_cb.setCurrentIndex(s.trig_mode)
        self.tedge_cb.setCurrentIndex(s.trig_edge)
        self.tsrc_cb.setCurrentIndex(s.trig_source)
        if not self.level_sb.hasFocus():        # never overwrite a level being typed
            self.level_sb.setValue(s.trig_level)
        self.state_lbl.setText("\n".join(_state_lines(s)))
        if self.scope.frame is not None:        # a held frame gets no new frame signal
            self._frame_flags(self.scope.frame)

    def frame_notes(self, f: ScopeFrame) -> list:
        """STOPPED / held-record notes for a frame against the state in force."""
        s = self.st
        if s is None:
            return []
        notes = [] if s.running else ["STOPPED (held record)"]
        diffs = []
        if f.timebase_idx != s.timebase_code:
            diffs.append(f"timebase 0x{f.timebase_idx:02X} (now 0x{s.timebase_code:02X})")
        for w in f.waves:
            now = s.ch[w.channel].vdiv_idx
            if w.vdiv_idx != now:
                diffs.append(f"{w.channel_name} range {w.vdiv_idx} (now {now})")
        if diffs:
            notes.append("record taken at " + ", ".join(diffs))
        return notes

    def _event(self, text: str) -> None:
        if self._events:
            self._events.write(f"{time.time():.3f} {time.strftime('%H:%M:%S')} {text}\n")
            self._events.flush()

    # ── slots ────────────────────────────────────────────────────
    def _set_conn(self, state: str, detail: str) -> None:
        self._state = state
        self._event(f"connection {state} {detail}")
        color = {"connected": "#27ae60", "disconnected": "#eb5757"}.get(state, "#f2994a")
        text = {"connected": f"connected {detail}", "disconnected": "DISCONNECTED",
                "searching": "searching for the unit…"}.get(state, state)
        self.sb_conn.setText(f"<b style='color:{color}'>{text}</b>")
        self.sb_conn.setToolTip(detail)
        if state != "connected":
            self.sb_mode.setText("")
            self.settings.setEnabled(False)
            self.st = None
            if state == "disconnected":
                self.sb_note.setText(detail[:120])

    def _status(self, st: proto.Status) -> None:
        batt = f"{st.battery_pct}% {st.battery_mv} mV" if st.battery_known else "battery ?"
        if st.charging:
            batt += " CHG"
        self.sb_mode.setText(f"mode {st.mode_name} | {batt}")
        self.info.setText(f"firmware {st.fw_version}\nprotocol v{st.proto_version}\n"
                          f"uptime {st.uptime_ms / 1000:.0f} s\n"
                          f"capture {'real samples' if st.capture_ready else 'no capture yet'}\n"
                          f"USB stalls {st.usb_tx_stalls}, heals {st.usb_heals}")
        if self._follow_mode and st.mode != self._last_mode:
            if st.mode == 0:
                self.views.setCurrentWidget(self.scope)
            elif st.mode == 1:
                self.views.setCurrentWidget(self.meter)
        self._last_mode = st.mode

    def _frame(self, f: ScopeFrame) -> None:
        self.scope.set_frame(f)
        self._frame_flags(f)

    def _frame_flags(self, f: ScopeFrame) -> None:
        trust =[f"rate {f.timebase_tier}" + (f" {f.sample_rate_hz} S/s" if f.sample_rate_hz else ""),
                 "calibrated" if f.calibrated else "uncalibrated"]
        if f.waves[0].timebase_disagrees:
            trust.append("timebase disagrees")
        notes = self.frame_notes(f)
        text = f"#{f.frame_id} tb 0x{f.timebase_idx:02X} | " + ", ".join(trust)
        if notes:
            text += " | <b style='color:#eb5757'>" + "; ".join(notes) + "</b>"
        self.sb_flags.setText(text)

    def _note(self, text: str) -> None:
        self._event(f"note {text}")
        self.sb_note.setText(f"{time.strftime('%H:%M:%S')} {text}")

    def _job_done(self, job: str, ok: bool, msg: str) -> None:
        self._event(f"job {job} {'ok' if ok else 'FAILED'} {msg}")
        self._note(f"{job} {'ok' if ok else 'FAILED'}: {msg}")
        if job == "screenshot":
            self.shot.setEnabled(True)

    def _tick(self) -> None:
        self.ticks.tick()
        self.scope.redraw()
        self.meter.redraw()

    def _rates(self) -> None:
        link = self.scope.frames_in.value()
        draws = self.scope.draws.value()
        mrate = self.meter.updates.value()
        self.sb_rate.setText(f"link {link:.1f} frames/s | draw {draws:.1f}/s | meter {mrate:.1f}/s")
        if self._stats:
            s = self.worker.stats
            fid = self.scope.frame.frame_id if self.scope.frame else ""
            self._stats.writerow([f"{time.time():.1f}", self._state, self.worker.mode,
                                  self.worker.mask, f"{link:.2f}", f"{draws:.2f}",
                                  f"{self.ticks.value():.1f}", s["frames"], s["meter"], s["naks"],
                                  s["connects"], s["losses"], fid]
                                 + [("" if v is None else f"{v:.3f}")
                                    for ch in (0, 1) for v in self.scope.peaks.get(ch, (None, None))])
            self._stats_f.flush()

    def _channels_changed(self, *_):
        mask = sum(1 << i for i, cb in enumerate(self.ch) if cb.isChecked())
        if not mask:                            # at least one channel
            self.ch[0].setChecked(True)
            return
        self.worker.set_channels(mask)

    def _pause(self, on: bool) -> None:
        self.worker.paused = on
        self.pause.setText("Resume" if on else "Pause")

    def _screenshot(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Unit screenshot", time.strftime("screen_%Y%m%d_%H%M%S.png"), "PNG (*.png)")
        if path:
            self.shot.setEnabled(False)
            self.worker.screenshot(path, hold=self.hold_shot.isChecked())

    def _save_npz(self) -> None:
        f = self.scope.frame
        if f is None:
            self._note("no frame to save")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save frame", time.strftime("frame_%Y%m%d_%H%M%S.npz"), "NumPy (*.npz)")
        if path:
            from ..cli import write_capture_npz
            write_capture_npz(path, [(f.t_unix, list(f.waves))])
            self._note(f"saved {path}")

    def _grab(self, path: Optional[str] = None) -> None:
        path = path or time.strftime("gui_%Y%m%d_%H%M%S.png")
        self.grab().save(path)
        self._note(f"window image {path}")

    def closeEvent(self, e) -> None:
        self.timer.stop()
        self.worker.stop()
        self.meter.stop_log()
        if self._stats:
            self._stats_f.close()
        self._event("closed")
        if self._events:
            self._events.close()
        super().closeEvent(e)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openscope-gui", description=__doc__.splitlines()[0])
    p.add_argument("--port", help="serial port (default: find the unit by USB VID:PID)")
    p.add_argument("--channels", default="1", help="1, 2 or 1,2 (default 1)")
    p.add_argument("--stats", metavar="CSV", help="append link/draw rates once a second (bench)")
    p.add_argument("--events", metavar="TXT", help="append connection changes, notes and jobs (bench)")
    p.add_argument("--meter-log", metavar="CSV", help="start the meter CSV log at launch")
    p.add_argument("--grab", metavar="PNG", help="save a window image after --grab-after s")
    p.add_argument("--grab-after", type=float, default=10.0)
    p.add_argument("--quit-after", type=float, metavar="S", help="close after S seconds (bench)")
    a = p.parse_args(argv)
    from ..device import Device
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=False, foreground="#dddddd", background="#1e1e1e")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    opener = (lambda: Device.open(a.port)) if a.port else Device.open
    worker = Worker(opener)
    mask = proto.channel_mask(a.channels)
    win = MainWindow(worker, a.stats, a.events)
    if a.meter_log:
        win.meter.start_log(a.meter_log)
        win.meter.log_btn.blockSignals(True)
        win.meter.log_btn.setChecked(True)
        win.meter.log_btn.setText("Stop log")
        win.meter.log_btn.blockSignals(False)
    for i, cb in enumerate(win.ch):
        cb.setChecked(bool(mask & (1 << i)))
    worker.set_channels(mask)
    win.show()
    worker.start()
    if a.grab:
        QtCore.QTimer.singleShot(int(a.grab_after * 1000), lambda: win._grab(a.grab))
    if a.quit_after:
        QtCore.QTimer.singleShot(int(a.quit_after * 1000), win.close)
    rc = app.exec()
    worker.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
