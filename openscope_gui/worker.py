"""Reader thread: the only code in the GUI that touches the serial port.

The protocol has no request ids (link.py opens the port exclusive), so every
request — polls, button presses, screenshots — goes through this one thread,
in order. The GUI thread only queues jobs and receives signals.

One loop iteration (`step()`):
  * not connected: try `Device.open()`; on failure wait `retry_s` and report
    "searching" (once per state change, not every attempt);
  * connected: run queued jobs; poll STATUS (and SCOPE_STATE, stage-085) every
    `status_s`; in scope mode ask for one capture, in meter mode for one meter
    reading; emit only NEW data (a frame_id / update_count that changed), so a
    stopped or held scope does not repaint the same record as if it were live.

Scope sets (stage-085) are jobs too: `set("timebase", 0x10)` etc. call the
Device method on this thread, then read SCOPE_STATE and emit it, so the window
shows the state in force, never the value it asked for. A refusal is a failed
job with the reason. Firmware without GET_SCOPE_STATE is noted once and not
polled again until the next connect.

Stage-087: at connect the worker asks once for the device description
(GET_DESCRIPTION; `Device` caches it per connection) and emits it, or None
for firmware without it. `set("probe", ch, f)` sets the probe. The window can
choose the port (`set_port`, None = automatic) and go offline (`set_online`).
`request_info()` collects the `openscope info` data for a dialog; `shell()`
runs one debug-shell line, and refuses the I-40 commands (`cal restore`,
`mem write`).

NAKs are normal (NO_CAPTURE_DATA before the first capture, NOT_READY when no
tear-free copy could be taken or the meter is blank — G-220, G-230): they are
reported as notes and polled again. A lost port (unplug, reboot, IAP) drops
the Device and goes back to searching, so a replug recovers without a restart.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import numpy as np
from PySide6 import QtCore

from openscope import proto
from openscope.device import Device, DeviceError, Nak, Timeout

MODE_SCOPE, MODE_METER = 0, 1

# set(what, *args) -> Device method (stage-085, the 080 control surface)
SETTERS = {"mode": "set_mode", "timebase": "set_timebase", "vdiv": "set_vdiv",
           "trigger": "set_trigger", "run": "run_stop", "probe": "set_probe"}
# I-40: never sent to the unit, whatever the user types in the debug console.
FORBIDDEN_SHELL = ("cal restore", "mem write")


def shell_refused(line: str) -> bool:
    """True if `line` is an I-40 command (case and spacing ignored)."""
    norm = " ".join(line.lower().split())
    return any(norm == f or norm.startswith(f + " ") for f in FORBIDDEN_SHELL)


@dataclass(frozen=True)
class ScopeFrame:
    """One capture as numpy, plus the header fields the views need."""
    t_unix: float
    frame_id: int
    channels: Tuple[int, ...]       # 0 = CH1, 1 = CH2, in row order of `samples`
    samples: np.ndarray             # uint8, shape (len(channels), sample_count)
    sample_rate_hz: int             # 0 = no trustworthy rate
    timebase_idx: int
    head_skip: int
    flags: int                      # first channel's WAVEFORM flags
    timebase_tier: str              # "measured" / "provisional" / "none"
    calibrated: bool
    waves: tuple                    # the proto.Waveform objects, unchanged

    @classmethod
    def from_waves(cls, waves, t_unix: float) -> "ScopeFrame":
        w0 = waves[0]
        return cls(t_unix=t_unix, frame_id=w0.frame_id,
                   channels=tuple(w.channel for w in waves),
                   samples=np.array([np.frombuffer(w.samples, dtype=np.uint8) for w in waves]),
                   sample_rate_hz=w0.sample_rate_hz, timebase_idx=w0.timebase_idx,
                   head_skip=w0.head_skip, flags=w0.flags, timebase_tier=w0.timebase_tier,
                   calibrated=w0.calibrated, waves=tuple(waves))


@dataclass(frozen=True)
class MeterSample:
    t_unix: float
    reading: proto.MeterReading


class Worker(QtCore.QThread):
    connection = QtCore.Signal(str, str)    # state ("searching"/"connected"/"disconnected"), detail
    status = QtCore.Signal(object)          # proto.Status
    frame = QtCore.Signal(object)           # ScopeFrame (new frame_id only)
    meter = QtCore.Signal(object)           # MeterSample (new update_count only)
    note = QtCore.Signal(str)               # a NAK or soft error, for the status bar
    job_done = QtCore.Signal(str, bool, str)  # job ("press"/"screenshot"/"set"), ok, message
    scope_state = QtCore.Signal(object)     # proto.ScopeState (with STATUS, and after each set)
    description = QtCore.Signal(object)     # proto.Description at connect, or None (old firmware)
    set_done = QtCore.Signal(str, bool)     # setting name, ok (after each set job)
    info = QtCore.Signal(object)            # dict for the device information dialog
    shell_out = QtCore.Signal(str, str)     # command, output (or the error text)

    MAX_TIMEOUTS = 3        # consecutive request timeouts before the link counts as lost

    def __init__(self, opener: Callable[[], Device] = Device.open, *,
                 status_s: float = 0.5, retry_s: float = 1.0, idle_s: float = 0.03,
                 meter_s: float = 0.1, nak_s: float = 0.15,
                 clock: Callable[[], float] = time.monotonic, parent=None):
        super().__init__(parent)
        self._opener = opener
        self.status_s, self.retry_s, self.idle_s = status_s, retry_s, idle_s
        self.meter_s, self.nak_s = meter_s, nak_s
        self._clock = clock
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._halt = threading.Event()
        self.dev: Optional[Device] = None
        self.mask = proto.WAVE_MASK_CH1     # set from the GUI thread (an int: atomic)
        self.paused = False
        self.mode: Optional[int] = None
        self._state = ""
        self._last_status = -1e9
        self._last_fid: Optional[int] = None
        self._last_uc: Optional[int] = None
        self._timeouts = 0
        self.has_state: Optional[bool] = None   # None = not asked yet on this connection
        self._need_desc = False
        self.port: Optional[str] = None     # None = automatic (VID:PID); read by the opener
        self.online = True                  # False = stay disconnected (Device > Disconnect)
        self.stats = {"frames": 0, "meter": 0, "naks": 0, "connects": 0, "losses": 0}

    # ── called from the GUI thread ────────────────────────────────
    def set_channels(self, mask: int) -> None:
        proto.channel_mask([c for c in (1, 2) if mask & (1 << (c - 1))])   # validates 1..3
        self.mask = mask
        self._last_fid = None           # a new channel set repaints even a held record

    def press(self, button) -> None:
        self._jobs.put(("press", proto.button_id(button)))

    def screenshot(self, path: str, hold: bool = False) -> None:
        """hold=True: STOP the scope for the dump if it runs, then RUN again
        (a held screen passes the CRC; a live one is torn, G-260)."""
        self._jobs.put(("screenshot", (path, hold)))

    def set(self, what: str, *args) -> None:
        """Queue one scope set: mode, timebase, vdiv, trigger or run (stage-085)."""
        if what not in SETTERS:
            raise ValueError(f"unknown setting {what!r}; one of {', '.join(SETTERS)}")
        self._jobs.put(("set", (what, args)))

    def set_probe(self, channel: int, factor: int) -> None:
        self.set("probe", channel, factor)

    def set_port(self, port: Optional[str]) -> None:
        """Use this port from the next connect (None = automatic); reconnects now."""
        self.port = port
        self._jobs.put(("reconnect", None))

    def set_online(self, on: bool) -> None:
        self.online = on

    def request_info(self) -> None:
        self._jobs.put(("info", None))

    def shell(self, line: str) -> None:
        if shell_refused(line):
            raise ValueError(f"'{line}' is never sent to the unit (I-40)")
        self._jobs.put(("shell", line))

    def stop(self, wait_ms: int = 5000) -> bool:
        self._halt.set()
        return self.wait(wait_ms) if self.isRunning() else True

    # ── the thread ────────────────────────────────────────────────
    def run(self) -> None:
        try:
            while not self._halt.is_set():
                self.step()
        finally:
            self._drop(None)

    def _pause(self, s: float) -> None:
        self._halt.wait(s)

    def _set_state(self, state: str, detail: str) -> None:
        if state != self._state or state == "connected":
            self._state = state
            self.connection.emit(state, detail)

    def _drop(self, why: Optional[str]) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
            if why is not None:
                self.stats["losses"] += 1
                self._set_state("disconnected", why)
        self.mode = None

    def step(self) -> None:
        if not self.online:
            if self.dev is not None:
                self._drop(None)
            self._fail_queued_jobs("disconnected by the user")
            self._set_state("offline", "")
            self._pause(self.retry_s)
            return
        if self.dev is None:
            self._fail_queued_jobs("not connected")
            try:
                self.dev = self._opener(self.port) if self.port else self._opener()
            except Exception as e:          # NoDevice, busy port, version refusal
                self._set_state("searching", str(e))
                self._pause(self.retry_s)
                return
            self.stats["connects"] += 1
            self._last_status, self._last_fid, self._last_uc, self._timeouts = -1e9, None, None, 0
            self.has_state = None
            self._need_desc = True
            self._set_state("connected", getattr(self.dev.link, "port", "") or "")
        try:
            if self._need_desc:
                self._get_description()
            self._run_jobs()
            if self.dev is None:            # a reconnect job dropped the device
                return
            if self._clock() - self._last_status >= self.status_s:
                st = self.dev.status()
                self._last_status = self._clock()
                self.mode = st.mode
                self.status.emit(st)
                self._poll_state()
            if self.paused:
                self._pause(self.idle_s)
            elif self.mode == MODE_SCOPE:
                self._poll_scope()
            elif self.mode == MODE_METER:
                self._poll_meter()
            else:
                self._pause(self.idle_s)
            self._timeouts = 0
        except Nak as e:
            self._timeouts = 0
            self.stats["naks"] += 1
            self.note.emit(proto.ERRORS.get(e.code, hex(e.code)))
            self._pause(self.nak_s)
        except Timeout as e:
            self._timeouts += 1
            if self._timeouts >= self.MAX_TIMEOUTS:
                self._drop(f"{self._timeouts} timeouts in a row: {e}")
            else:
                self.note.emit(f"timeout: {e}")
        except proto.ProtocolError as e:
            self.note.emit(f"protocol: {e}")
            self._pause(self.nak_s)
        except Exception as e:              # DeviceError, NoDevice from a failed reopen, OSError
            self._drop(f"{type(e).__name__}: {e}")

    def _poll_scope(self) -> None:
        waves = self.dev.waveform(self.mask)
        if waves[0].frame_id != self._last_fid:
            self._last_fid = waves[0].frame_id
            self.stats["frames"] += 1
            self.frame.emit(ScopeFrame.from_waves(waves, time.time()))
        else:
            self._pause(self.idle_s)

    def _get_description(self) -> None:
        """Once per connection. A NAK means firmware before stage-086."""
        fn = getattr(self.dev, "description", None)
        if fn is None:
            self._need_desc = False
            self.description.emit(None)
            return
        try:
            d = fn()
        except Nak as e:
            self._need_desc = False
            self.note.emit(f"no device description on this firmware "
                           f"({proto.ERRORS.get(e.code, hex(e.code))})")
            self.description.emit(None)
            return
        self._need_desc = False
        self.description.emit(d)

    def _poll_state(self) -> None:
        """SCOPE_STATE, unless this firmware has shown it has none."""
        if self.has_state is False:
            return
        try:
            s = self.dev.scope_state()
        except Nak as e:
            if self.has_state is None:          # older image: no control surface
                self.has_state = False
                self.note.emit(f"no scope controls on this firmware "
                               f"(GET_SCOPE_STATE: {proto.ERRORS.get(e.code, hex(e.code))})")
                return
            raise
        self.has_state = True
        self.scope_state.emit(s)

    def _poll_meter(self) -> None:
        m = self.dev.meter()
        if m.update_count != self._last_uc:
            self._last_uc = m.update_count
            self.stats["meter"] += 1
            self.meter.emit(MeterSample(time.time(), m))
        self._pause(self.meter_s)

    def _run_jobs(self) -> None:
        while True:
            try:
                job, arg = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job == "reconnect":
                self._drop(None)
                self._fail_queued_jobs("reconnecting")
                self._set_state("searching", "port changed")
                return
            try:
                if job == "press":
                    self.dev.press(arg)
                    self.job_done.emit(job, True, proto_button_name(arg))
                    self._last_status = -1e9        # a press may change the mode: poll now
                elif job == "screenshot":
                    self._screenshot_job(*arg)
                elif job == "set":
                    what, args = arg
                    try:
                        getattr(self.dev, SETTERS[what])(*args)
                    except Exception:
                        self.set_done.emit(what, False)
                        raise
                    self.job_done.emit(job, True, f"{what} {' '.join(str(a) for a in args)}".strip())
                    self.set_done.emit(what, True)
                    if what == "mode":
                        self._last_status = -1e9    # STATUS knows the mode first: poll now
                    self._poll_state()
                elif job == "info":
                    self._info_job()
                elif job == "shell":
                    try:
                        out = self.dev.shell(arg, timeout=5.0)
                    except Exception as e:
                        self.shell_out.emit(arg, f"error: {e}")
                        raise
                    self.shell_out.emit(arg, out)
                    self.job_done.emit(job, True, arg)
            except Nak as e:
                name = proto.ERRORS.get(e.code, hex(e.code))
                what = f"{arg[0]} " if job == "set" else ""
                self.job_done.emit(job, False, f"{what}refused: {name}: {proto.NAK_HINTS.get(name, '')}".rstrip(": "))
            except (DeviceError, OSError) as e:
                # Not a link loss by itself: the next STATUS poll decides that.
                self.job_done.emit(job, False, f"{type(e).__name__}: {e}")

    def _info_job(self) -> None:
        version = self.dev.ping()
        st = self.dev.status()
        state = desc = None
        if self.has_state:
            state = self.dev.scope_state()
        try:
            desc = self.dev.description()
        except Nak:
            pass
        self.info.emit({"version": version, "port": getattr(self.dev.link, "port", "") or "",
                        "status": st, "state": state, "description": desc})
        self.job_done.emit("info", True, "")

    def _screenshot_job(self, path: str, hold: bool) -> None:
        from openscope.screen import save_png
        held = False
        try:
            if hold and self.has_state:
                if self.dev.scope_state().running:
                    self.dev.run_stop(False)
                    held = True
            s = self.dev.screenshot()
        finally:
            if held:
                self.dev.run_stop(True)     # a failed dump must not leave the scope stopped
        if held or hold:
            self._poll_state()
        save_png(path, s.w, s.h, s.indexed4, scale=2)
        crc = "transport CRC ok, screen changed during capture" if s.torn else "CRC ok"
        how = ", held" if held else ""
        self.job_done.emit("screenshot", True, f"{path} ({s.w}x{s.h}, {crc}{how})")

    def _fail_queued_jobs(self, why: str) -> None:
        while True:
            try:
                job, arg = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job == "reconnect":
                continue
            if job == "set":
                self.set_done.emit(arg[0], False)
            if job == "shell":
                self.shell_out.emit(arg, f"error: {why}")
            self.job_done.emit(job, False, why)


def proto_button_name(bid: int) -> str:
    return next((k for k, v in proto.BUTTONS.items() if v == bid), str(bid))
