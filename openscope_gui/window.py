"""Main window of `openscope-gui` (stage-087): tabs are unit modes, one control
panel per tab, menus, toolbar, a hidden unit keys dock, settings kept with
QSettings.

Tabs: Oscilloscope, Multimeter, Signal generator. A user change of tab sends
SET_MODE; a mode change on the unit selects the tab with signals blocked, so
it sends nothing. While the unit shows its Settings mode (no tab) a line over
the tabs says so and the tab stays. Between a user tab change and the end of
its SET_MODE job, STATUS polls do not move the tab (they can still carry the
old mode).

The window shows the state the unit reports back, never the value it asked
for. Visible text is in words and units (labels.py); codes are in tooltips.
"""
from __future__ import annotations

import csv
import os
import time
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from openscope import proto
from . import dialogs, labels
from .scope_panel import ScopePanel, continuous
from .views import MeterView, Rate, ScopeView
from .worker import ScopeFrame, Worker

DRAW_HZ = 30
KEYS = [["CH1", "CH2", "MOVE", "SELECT", "TRIGGER"],
        ["PRM", "AUTO", "SAVE", "MENU", "OK"],
        ["UP", "DOWN", "LEFT", "RIGHT"]]          # POWER left out on purpose (shuts the unit down)
TAB_MODES = (0, 1, 2)                             # tab index == unit mode id
MODE_TIMEOUT_MS = 8000                            # longest SET_MODE (5 s) + margin
SETTINGS_BANNER = "The unit shows its settings menu"
PLACEHOLDER = {1: "Multimeter function controls come in stage 088.",
               2: "Signal generator controls come in stage 089."}
VIEW_DEFAULTS = {"view/control_panel": True, "view/unit_keys": False,
                 "view/uncalibrated": False, "view/advanced": False}


def _placeholder(text: str) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    lbl = QtWidgets.QLabel(text)
    lbl.setWordWrap(True)
    lay.addWidget(lbl)
    lay.addStretch(1)
    return w


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, worker: Worker, stats_path: Optional[str] = None,
                 events_path: Optional[str] = None, settings: Optional[QtCore.QSettings] = None):
        super().__init__()
        self.worker = worker
        self.settings = settings if settings is not None else QtCore.QSettings("OpenScope", "openscope-gui")
        self.setWindowTitle("OpenScope 2C53T")
        self.resize(1200, 760)
        self.st: Optional[proto.ScopeState] = None
        self.desc: Optional[proto.Description] = None
        self._state = "searching"
        self._unit_mode: Optional[int] = None
        self._pending_mode: Optional[int] = None
        self._fw = ""
        self._events = open(events_path, "a", encoding="utf-8") if events_path else None
        self._info_dlg = None
        self._console = None
        self.ticks = Rate()

        # central: settings banner over the mode tabs
        self.scope = ScopeView()
        self.meter = MeterView()
        self.gen_view = _placeholder("The unit is in signal generator mode. " + PLACEHOLDER[2])
        self.tabs = QtWidgets.QTabWidget()
        for w, name in ((self.scope, "Oscilloscope"), (self.meter, "Multimeter"),
                        (self.gen_view, "Signal generator")):
            self.tabs.addTab(w, name)
        for i in TAB_MODES:
            self.tabs.setTabToolTip(i, f"Puts the unit in {labels.MODE_WORDS[i].lower()} mode")
        self.banner = QtWidgets.QLabel(SETTINGS_BANNER)
        self.banner.setStyleSheet("background:#f2994a; color:black; padding:4px; font-weight:bold")
        self.banner.hide()
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.banner)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # control panel dock: one panel per tab
        self.panel = ScopePanel(worker)
        self.panel.channels_changed.connect(self.worker.set_channels)
        self.panel.level_50.connect(self._level_50)
        self.scope.level_dragged.connect(self.panel.set_level_counts)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.panel)
        self.stack.addWidget(_placeholder(PLACEHOLDER[1]))
        self.stack.addWidget(_placeholder(PLACEHOLDER[2]))
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.stack)
        self.control_dock = QtWidgets.QDockWidget("Control panel", self)
        self.control_dock.setObjectName("control_dock")
        self.control_dock.setWidget(scroll)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.control_dock)
        self.tabs.currentChanged.connect(self._tab_changed)

        # unit keys dock: hidden by default
        keys = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(keys)
        self.key_buttons = []
        for r, row in enumerate(KEYS):
            for c, name in enumerate(row):
                b = QtWidgets.QPushButton(name)
                b.setToolTip(f"Sends a {name} key press to the unit")
                b.clicked.connect(lambda _=False, n=name: self.worker.press(n))
                g.addWidget(b, r, c)
                self.key_buttons.append(b)
        self.keys_dock = QtWidgets.QDockWidget("Unit keys (sends key presses)", self)
        self.keys_dock.setObjectName("keys_dock")
        self.keys_dock.setWidget(keys)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.keys_dock)
        self.keys_dock.hide()

        self._build_actions()
        self._build_menus()
        self._build_toolbar()

        # status bar
        self.sb_conn = QtWidgets.QLabel()
        self.sb_mode = QtWidgets.QLabel()
        self.sb_scope = QtWidgets.QLabel()
        self.sb_rate = QtWidgets.QLabel()
        self.sb_note = QtWidgets.QLabel()
        for w in (self.sb_conn, self.sb_mode, self.sb_scope, self.sb_rate):
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
        worker.description.connect(self._description)
        worker.set_done.connect(self._set_done)
        worker.info.connect(self._info)
        self.meter.log_btn.toggled.connect(self._meter_log_toggled)
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / DRAW_HZ))
        self.rate_timer = QtCore.QTimer(self)
        self.rate_timer.timeout.connect(self._rates)
        self.rate_timer.start(1000)
        self.mode_timer = QtCore.QTimer(self)
        self.mode_timer.setSingleShot(True)
        self.mode_timer.timeout.connect(lambda: self._set_done("mode", False))
        self._stats = None
        if stats_path:
            self._stats_f = open(stats_path, "a", newline="")
            self._stats = csv.writer(self._stats_f)
            if self._stats_f.tell() == 0:
                self._stats.writerow(["t_unix", "state", "mode", "channels", "link_fps",
                                      "draws_per_s", "timer_hz", "frames", "meter", "naks",
                                      "connects", "losses", "frame_id",
                                      "ch1_fft_hz", "ch1_period_hz", "ch2_fft_hz", "ch2_period_hz"])
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+G"), self, activated=self._grab)
        self.restore_settings()

    # ── actions, menus, toolbar ────────────────────────────────────
    def _action(self, text, slot=None, shortcut=None, icon=None, checkable=False, tip=None):
        a = QtGui.QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
        if slot is not None:
            (a.toggled if checkable else a.triggered).connect(slot)
        return a

    def _build_actions(self) -> None:
        SP = QtWidgets.QStyle
        self.act_shot = self._action("Save LCD screenshot…", self._screenshot, "Ctrl+P",
                                     SP.SP_ComputerIcon, tip="Save the unit's screen as PNG (Ctrl+P)")
        self.act_save = self._action("Save waveform…", self._save_waveform, "Ctrl+S",
                                     SP.SP_DialogSaveButton, tip="Save the shown record as CSV or NPZ (Ctrl+S)")
        self.act_meter_log = self._action("Record meter to CSV…", self._record_meter, checkable=True)
        self.act_exit = self._action("Exit", self.close, "Ctrl+Q")
        self.act_run = self._action("Run/Stop", self.panel.toggle_run, "Space", SP.SP_MediaPause,
                                    tip="Run or stop the acquisition (Space)")
        self.act_single = self._action("Single", self.panel.single, None, SP.SP_MediaSkipForward,
                                       tip="Capture one triggered record, then hold it")
        self.act_pause = self._action("Pause display", self._pause, None, SP.SP_MediaPause,
                                      checkable=True, tip="Stop reading the unit (the unit keeps running)")
        self.act_control = self.control_dock.toggleViewAction()
        self.act_control.setText("Control panel")
        self.act_keys = self.keys_dock.toggleViewAction()
        self.act_keys.setText("Unit keys")
        self.act_uncal = self._action("Show uncalibrated settings", self._show_uncal, checkable=True)
        self.act_advanced = self._action("Advanced tools", self._advanced, checkable=True)
        self.act_connect = self._action("Connect", lambda: self._online(True))
        self.act_disconnect = self._action("Disconnect", lambda: self._online(False))
        self.act_info = self._action("Device information…", self._device_info)
        self.act_console = self._action("Debug console…", self._debug_console)
        self.act_about = self._action("About", lambda: dialogs.show_about(self, self._fw), "F1")
        for a in (self.act_run, self.act_single):
            a.setEnabled(False)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.act_shot, self.act_save, self.act_meter_log):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_exit)
        m = mb.addMenu("&View")
        for a in (self.act_control, self.act_keys, self.act_uncal, self.act_advanced):
            m.addAction(a)
        m = mb.addMenu("&Device")
        m.addAction(self.act_connect)
        m.addAction(self.act_disconnect)
        self.port_menu = m.addMenu("Port")
        self.port_group = QtGui.QActionGroup(self)
        self.port_menu.aboutToShow.connect(self._fill_ports)
        m.addSeparator()
        m.addAction(self.act_info)
        self.tools_menu = mb.addMenu("&Tools")
        self.tools_menu.addAction(self.act_console)
        self.tools_menu.addAction(self.act_keys)
        self.tools_menu.menuAction().setVisible(False)
        m = mb.addMenu("&Help")
        m.addAction(self.act_about)
        self._fill_ports()

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Toolbar")
        tb.setObjectName("main_toolbar")
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
        tb.setMovable(False)
        for a in (self.act_run, self.act_single, None, self.act_shot, self.act_save, None, self.act_pause):
            if a is None:
                tb.addSeparator()
            else:
                tb.addAction(a)
        # short texts under the icons; the menu items keep their full names
        for a, text in ((self.act_shot, "Screenshot"), (self.act_save, "Save waveform")):
            tb.widgetForAction(a).setText(text)
        self.toolbar = tb

    # ── settings (QSettings) ───────────────────────────────────────
    def _flag(self, key: str) -> bool:
        return bool(self.settings.value(key, VIEW_DEFAULTS[key], type=bool))

    def restore_settings(self) -> None:
        geo = self.settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        st = self.settings.value("window/state")
        if st is not None:
            self.restoreState(st)
        self.control_dock.setVisible(self._flag("view/control_panel"))
        self.keys_dock.setVisible(self._flag("view/unit_keys"))
        self.act_uncal.setChecked(self._flag("view/uncalibrated"))
        self.act_advanced.setChecked(self._flag("view/advanced"))

    def save_settings(self) -> None:
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/state", self.saveState())
        s.setValue("view/control_panel", not self.control_dock.isHidden())
        s.setValue("view/unit_keys", not self.keys_dock.isHidden())
        s.setValue("view/uncalibrated", self.act_uncal.isChecked())
        s.setValue("view/advanced", self.act_advanced.isChecked())
        s.sync()

    # ── modes and tabs ─────────────────────────────────────────────
    def _tab_changed(self, idx: int) -> None:
        """A user change of tab (programmatic follows block this signal)."""
        self.stack.setCurrentIndex(idx)
        if self._state != "connected" or idx == self._unit_mode:
            return
        self._pending_mode = idx
        self.mode_timer.start(MODE_TIMEOUT_MS)
        self._event(f"user tab {labels.MODE_WORDS[idx]}")
        self.worker.set("mode", proto.MODES[idx])

    def _follow_mode(self, mode: int) -> None:
        """The unit's mode: select its tab, sending nothing."""
        self._unit_mode = mode
        self.banner.setVisible(mode == 3)
        if self._pending_mode is not None or mode not in TAB_MODES:
            return
        if self.tabs.currentIndex() != mode:
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(mode)
            self.tabs.blockSignals(False)
            self.stack.setCurrentIndex(mode)
            self._event(f"tab follows unit: {labels.MODE_WORDS[mode]}")

    def _set_done(self, what: str, ok: bool) -> None:
        if what != "mode" or self._pending_mode is None:
            return
        self._pending_mode = None
        self.mode_timer.stop()
        if not ok and self._unit_mode is not None:
            self._follow_mode(self._unit_mode)          # go back to the unit's tab

    # ── worker slots ───────────────────────────────────────────────
    def _event(self, text: str) -> None:
        if self._events:
            self._events.write(f"{time.time():.3f} {time.strftime('%H:%M:%S')} {text}\n")
            self._events.flush()

    def _set_conn(self, state: str, detail: str) -> None:
        self._state = state
        self._event(f"connection {state} {detail}")
        color = {"connected": "#27ae60", "disconnected": "#eb5757"}.get(state, "#f2994a")
        text = {"connected": f"Connected ({detail})" if detail else "Connected",
                "disconnected": "Connection lost", "offline": "Disconnected",
                "searching": "Looking for the unit…"}.get(state, state)
        self.sb_conn.setText(f"<b style='color:{color}'>{text}</b>")
        self.sb_conn.setToolTip(detail)
        self.act_connect.setEnabled(state == "offline")
        self.act_disconnect.setEnabled(state != "offline")
        if state != "connected":
            self.sb_mode.setText("")
            self.sb_scope.setText("")
            self.st = None
            self.desc = None
            self._unit_mode = None
            self._pending_mode = None
            self.banner.hide()
            self.panel.clear()
            self.panel.set_description(None)
            self._actions_enabled(False)
            if state == "disconnected":
                self.sb_note.setText(labels.note_words(detail)[:120])
                self.sb_note.setToolTip(detail)

    def _actions_enabled(self, on: bool) -> None:
        self.act_run.setEnabled(on)
        self.act_single.setEnabled(on)

    def _status(self, st: proto.Status) -> None:
        self._fw = st.fw_version
        self.sb_mode.setText(f"{labels.MODE_WORDS.get(st.mode, 'Unknown mode')}{labels.SEP}"
                             f"{labels.battery_words(st)}")
        self.sb_mode.setToolTip(f"Unit mode {st.mode}, battery {st.battery_mv} mV, "
                                f"USB stalls {st.usb_tx_stalls}, heals {st.usb_heals}")
        self._follow_mode(st.mode)

    def _description(self, d: Optional[proto.Description]) -> None:
        self.desc = d
        self.panel.set_description(d)
        self._update_scope_status()

    def _scope_state(self, s: proto.ScopeState) -> None:
        self.st = s
        self.panel.set_state(s)
        self._actions_enabled(True)
        self.act_run.setIcon(self.style().standardIcon(
            QtWidgets.QStyle.SP_MediaPause if continuous(s) else QtWidgets.QStyle.SP_MediaPlay))
        self._follow_mode(s.mode)
        self._update_scope_status()

    def _frame(self, f: ScopeFrame) -> None:
        self.scope.set_frame(f)
        self._update_scope_status()

    def frame_notes(self, f: ScopeFrame) -> list:
        return labels.frame_notes(f, self.st, self.desc)

    def _update_scope_status(self) -> None:
        self.scope.set_trigger_level(labels.trigger_counts(self.desc, self.st))
        f = self.scope.frame
        text = labels.scope_status(self.st, self.desc, f)
        self.sb_scope.setText(text)
        warn = f is not None and any(n.startswith(("held record", "single record", "record taken",
                                                   "trigger level outside", "the unit triggers on CH1"))
                                     for n in self.frame_notes(f))
        self.sb_scope.setStyleSheet("color:#eb5757; font-weight:bold" if warn else "")
        tip = []
        if f is not None:
            tip.append(f"Frame {f.frame_id}, timebase code 0x{f.timebase_idx:02X}, "
                       f"{f.sample_rate_hz} samples/s, per-unit calibration: "
                       f"{'yes' if f.calibrated else 'no'}")
        if self.st is not None:
            tip.append(f"In force: timebase code 0x{self.st.timebase_code:02X}, trigger register "
                       f"0x{self.st.trig_code:02X}")
        self.sb_scope.setToolTip("\n".join(tip))

    def _note(self, text: str) -> None:
        self._event(f"note {text}")
        self.sb_note.setText(f"{time.strftime('%H:%M:%S')} {labels.note_words(text)}")
        self.sb_note.setToolTip(text)

    def _job_done(self, job: str, ok: bool, msg: str) -> None:
        self._event(f"job {job} {'ok' if ok else 'FAILED'} {msg}")
        if job == "info":
            if not ok and self._info_dlg is not None:
                self._info_dlg.failed(msg)
            return
        if job == "shell":
            return
        self.sb_note.setText(f"{time.strftime('%H:%M:%S')} {labels.job_words(job, ok, msg)}")
        self.sb_note.setToolTip(msg)
        if job == "screenshot":
            self.act_shot.setEnabled(True)

    def _info(self, info: dict) -> None:
        if self._info_dlg is not None:
            self._info_dlg.show_info(info)

    def _tick(self) -> None:
        self.ticks.tick()
        self.scope.redraw()
        self.meter.redraw()

    def _rates(self) -> None:
        link = self.scope.frames_in.value()
        draws = self.scope.draws.value()
        mrate = self.meter.updates.value()
        self.sb_rate.setText(f"link {link:.1f} frames/s · draw {draws:.1f}/s · meter {mrate:.1f}/s")
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

    # ── user actions ───────────────────────────────────────────────
    def _level_50(self) -> None:
        f = self.scope.frame
        rows = [i for i, c in enumerate(f.channels) if c == 0] if f is not None else []
        if not rows:
            self._note("Show CH1 to set the level from its signal")
            return
        body = f.samples[rows[0]][f.head_skip:]
        self.panel.set_level_counts((int(body.min()) + int(body.max())) / 2)

    def _pause(self, on: bool) -> None:
        self.worker.paused = on
        self.act_pause.setText("Resume display" if on else "Pause display")

    def _show_uncal(self, on: bool) -> None:
        self.panel.set_show_uncalibrated(on)

    def _advanced(self, on: bool) -> None:
        self.tools_menu.menuAction().setVisible(on)

    def _online(self, on: bool) -> None:
        self.worker.set_online(on)
        self.act_connect.setEnabled(False)
        self.act_disconnect.setEnabled(on)

    def _fill_ports(self) -> None:
        from openscope.link import candidate_ports
        self.port_menu.clear()
        for a in self.port_group.actions():
            self.port_group.removeAction(a)
        try:
            ports = candidate_ports()
        except Exception:
            ports = []
        if self.worker.port and self.worker.port not in ports:
            ports.append(self.worker.port)
        for name, port in [("Automatic", None)] + [(p, p) for p in ports]:
            a = QtGui.QAction(name, self.port_group)
            a.setCheckable(True)
            a.setChecked(port == self.worker.port)
            a.triggered.connect(lambda _=False, p=port: self.worker.set_port(p))
            self.port_menu.addAction(a)

    def _device_info(self) -> None:
        self._info_dlg = dialogs.DeviceInfoDialog(self)
        self._info_dlg.finished.connect(lambda _: setattr(self, "_info_dlg", None))
        self._info_dlg.show()
        self.worker.request_info()

    def _debug_console(self) -> None:
        if self._console is None:
            self._console = dialogs.DebugConsoleDialog(self.worker, self)
        self._console.show()
        self._console.raise_()

    def _record_meter(self, on: bool) -> None:
        if self.meter.log_btn.isChecked() != on:
            self.meter.log_btn.setChecked(on)       # the meter view asks for the file

    def _meter_log_toggled(self, on: bool) -> None:
        self.act_meter_log.blockSignals(True)
        self.act_meter_log.setChecked(on)
        self.act_meter_log.blockSignals(False)

    def _screenshot(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save LCD screenshot", time.strftime("screen_%Y%m%d_%H%M%S.png"), "PNG (*.png)")
        if path:
            self.act_shot.setEnabled(False)
            self.worker.screenshot(path, hold=True)     # held screen: CRC ok (G-260)

    def _save_waveform(self) -> None:
        f = self.scope.frame
        if f is None:
            self._note("No record to save yet")
            return
        path, flt = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save waveform", time.strftime("waveform_%Y%m%d_%H%M%S.csv"),
            "CSV (*.csv);;NumPy (*.npz)")
        if path:
            self.save_waveform(path, flt)

    def save_waveform(self, path: str, flt: str = "") -> str:
        """CSV or NPZ by the file type (the extension, else the chosen filter)."""
        from openscope.cli import write_capture_csv, write_capture_npz
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".csv", ".npz"):
            ext = ".npz" if "npz" in flt else ".csv"
            path += ext
        f = self.scope.frame
        cap = [(f.t_unix, list(f.waves))]
        if ext == ".npz":
            write_capture_npz(path, cap)
        else:
            write_capture_csv(path, cap)
        self._note(f"Waveform saved: {path}")
        return path

    def _grab(self, path: Optional[str] = None) -> None:
        path = path or time.strftime("gui_%Y%m%d_%H%M%S.png")
        self.grab().save(path)
        self._note(f"Window image saved: {path}")

    def closeEvent(self, e) -> None:
        self.save_settings()
        self.timer.stop()
        self.worker.stop()
        self.meter.stop_log()
        if self._stats:
            self._stats_f.close()
        self._event("closed")
        if self._events:
            self._events.close()
        super().closeEvent(e)
