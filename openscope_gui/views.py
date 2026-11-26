"""Scope and meter views (pyqtgraph). They only draw: no port access here.

Honesty rules carried over from the library: samples are raw ADC counts
(no per-unit calibration, no zero point), so the vertical axis says so; time
and frequency are shown only when the frame carries a sample rate, otherwise
the axis is in samples. Samples [0, head_skip) are the record-head defect and
are shaded and left out of every measurement.

Stage-260: the meter is a MeterModel (data, blank state, CSV log) and a
MeterView (the widget that draws from it). Stage-275: the model has the host
tools (Hold, Relative, minimum/maximum/average) and a "function" CSV column.
"""
from __future__ import annotations

import csv
import os
import re
import time
from collections import deque
from typing import Dict, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import labels
from .worker import MeterSample, ScopeFrame

CH_COLORS = ("#f2c94c", "#56ccf2")      # CH1 yellow, CH2 cyan (the unit's own colours)
FFT_PAD = 8192
MIN_SPAN = 4        # counts: below this a record is flat/noise (proto.estimate_period's rule)


# ── analysis helpers (pure numpy; unit-testable) ──────────────────────
def fft_peak(x: np.ndarray, fs: Optional[float]):
    """Spectrum of one record body and its strongest non-DC peak.
    Returns (freqs, mag_db, peak_freq). Hann window, zero-padded to FFT_PAD,
    parabolic interpolation on the log magnitude around the peak bin.
    Frequencies in Hz when fs is known, else in cycles/sample.
    peak_freq is None when the strongest bin above the low cut is not the
    maximum of its own main lobe (a ramp or a slow drift: the spectrum only
    falls from DC; one edge of a slow square), and the interpolation step stays
    within half a bin (Q-9)."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 16 or np.ptp(x) < MIN_SPAN:
        return None, None, None
    w = np.hanning(n)
    spec = np.abs(np.fft.rfft((x - x.mean()) * w, FFT_PAD))
    unit = fs if fs else 1.0
    freqs = np.fft.rfftfreq(FFT_PAD, 1.0 / unit)
    db = 20 * np.log10(spec / spec.max() + 1e-12)
    lo = int(np.ceil(2 * FFT_PAD / n))          # skip DC and the window's main lobe around it
    k = lo + int(np.argmax(spec[lo:]))
    # a tone is the maximum of its own Hann main lobe (+-2 natural bins = +-lo padded
    # bins); a ramp, or a sidelobe of the leakage of one edge (a record shorter than a
    # period, stage-255 bench: "35 Hz" for 10 Hz at 0x10), is not
    if not (0 < k < len(spec) - 1 and spec[k] >= spec[max(0, k - lo):k + lo + 1].max()):
        return freqs, db, None
    a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
    den = a - 2 * b + c
    d = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5)) if den else 0.0
    return freqs, db, (k + d) * unit / FFT_PAD


def rising_crossings(x: np.ndarray) -> np.ndarray:
    """Sub-sample positions of the rising crossings of the 5-95 % midpoint,
    with hysteresis of a quarter of that span (proto.estimate_period's rule):
    the signal must fall below mid - span/4 before the next crossing counts, so
    noise near the level cannot add crossings (Q-9). Empty when flat."""
    x = np.asarray(x, float)
    if len(x) < 8:
        return np.empty(0)
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    if hi - lo < MIN_SPAN:
        return np.empty(0)
    mid = (lo + hi) / 2
    low = mid - (hi - lo) / 4
    out = []
    armed = False
    for i in range(1, len(x)):
        a, b = x[i - 1], x[i]
        if b <= low:
            armed = True
        if armed and a < mid <= b:
            out.append(i - 1 + (mid - a) / (b - a))
            armed = False
    return np.array(out)


def median_period(x: np.ndarray) -> Optional[float]:
    """Mean of the single periods (rising crossings with hysteresis, sub-sample)
    within 10 % of their median: a seam that cuts a period cannot bias it
    (G-240; same method as openscope-bench/capture_stats.py). In samples."""
    rr = rising_crossings(x)
    if len(rr) < 3:
        return None
    iv = np.diff(rr)
    med = np.median(iv)
    good = iv[np.abs(iv - med) < 0.1 * med]
    return float(good.mean()) if len(good) else None


def fmt_time(s: float) -> str:
    a = abs(s)
    if a >= 1:
        return f"{s:.4g} s"
    if a >= 1e-3:
        return f"{s * 1e3:.4g} ms"
    return f"{s * 1e6:.4g} µs"


def fmt_hz(f: float) -> str:
    a = abs(f)
    if a >= 1e5:
        return f"{f / 1e3:.2f} kHz"
    return f"{f:.2f} Hz" if a >= 10 else f"{f:.4f} Hz"


class Rate:
    """Events per second over a sliding window."""

    def __init__(self, window_s: float = 2.0):
        self.window = window_s
        self.t: deque = deque()

    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.t.append(now)
        while now - self.t[0] > self.window:    # bounded without value() calls
            self.t.popleft()

    def value(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        while self.t and now - self.t[0] > self.window:
            self.t.popleft()
        if len(self.t) < 2:
            return 0.0
        span = now - self.t[0]
        return (len(self.t) - 1) / span if span > 0 else 0.0


# ── scope ─────────────────────────────────────────────────────────────
class ScopeView(QtWidgets.QWidget):
    level_dragged = QtCore.Signal(float)    # ADC counts where the user dropped the trigger line

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame: Optional[ScopeFrame] = None
        self._dirty = False
        self._scale = 1.0           # x units per sample (ms per sample, or 1 for index)
        self._xunit = "sample"
        self.draws = Rate()
        self.frames_in = Rate()

        self.tabs = QtWidgets.QTabWidget()
        # time view
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "ADC counts (uncalibrated)")
        self.plot.setLabel("bottom", "sample index")
        self.plot.setYRange(0, 255, padding=0.02)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.addLegend(offset=(10, 10))
        self.curves = [self.plot.plot([], [], pen=pg.mkPen(c, width=1.5), name=f"CH{i + 1}")
                       for i, c in enumerate(CH_COLORS)]
        self.head = pg.LinearRegionItem((0, 0), movable=False,
                                        brush=pg.mkBrush(128, 128, 128, 50), pen=pg.mkPen(None))
        self.head.setZValue(-10)
        self.plot.addItem(self.head)
        self.cur = []
        for name, pos, col in (("A", 300, "#eb5757"), ("B", 600, "#6fcf97")):
            ln = pg.InfiniteLine(pos=pos, angle=90, movable=True, pen=pg.mkPen(col, width=1),
                                 label=name, labelOpts={"position": 0.95, "color": col})
            ln.sigPositionChanged.connect(self._update_readout)
            self.plot.addItem(ln)
            self.cur.append(ln)
        # trigger level (stage-087): where the hardware fires, in record counts; drag to set
        self.trig_line = pg.InfiniteLine(pos=128, angle=0, movable=True,
                                         pen=pg.mkPen(CH_COLORS[0], width=1, style=QtCore.Qt.DashLine),
                                         hoverPen=pg.mkPen("#ffffff", width=2),
                                         label="Trigger", labelOpts={"position": 0.03, "color": CH_COLORS[0]})
        self.trig_line.setToolTip("Trigger level (drag to change), compared with the trigger source.")
        self.trig_line.sigPositionChangeFinished.connect(lambda ln: self.level_dragged.emit(ln.value()))
        self.trig_line.hide()
        self.plot.addItem(self.trig_line)
        self.trig_mark = pg.InfiniteLine(pos=0, angle=90, movable=False,
                                         pen=pg.mkPen(CH_COLORS[0], width=1, style=QtCore.Qt.DotLine),
                                         label="T", labelOpts={"position": 0.03, "color": CH_COLORS[0]})
        self.trig_mark.hide()
        self.plot.addItem(self.trig_mark)
        self.tabs.addTab(self.plot, "Time")
        # FFT view
        self.fft = pg.PlotWidget()
        self.fft.showGrid(x=True, y=True, alpha=0.25)
        self.fft.setLabel("left", "magnitude (dB, relative to peak)")
        self.fft.setLabel("bottom", "frequency (cycles/sample)")
        self.fft.setYRange(-90, 5, padding=0)
        self.fft_curves = [self.fft.plot([], [], pen=pg.mkPen(c, width=1)) for c in CH_COLORS]
        self.tabs.addTab(self.fft, "FFT")

        self.readout = QtWidgets.QLabel("cursors: no data")
        self.meas = QtWidgets.QLabel("")
        for lab in (self.readout, self.meas):
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            lab.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.readout)
        lay.addWidget(self.meas)
        self.peaks = {}             # channel -> (fft peak, median-period f) of the last draw

    # data in (GUI thread, from the worker's signal)
    def set_frame(self, f: ScopeFrame) -> None:
        self.frame = f
        self._dirty = True
        self.frames_in.tick()

    def set_trigger_level(self, counts: Optional[float]) -> None:
        """Show the trigger level line at `counts` (None hides it). Never moves a
        line the user is dragging."""
        if counts is None:
            self.trig_line.hide()
            return
        if not self.trig_line.moving:
            self.trig_line.setValue(counts)
        self.trig_line.show()

    def clear(self) -> None:
        self.frame = None
        self._dirty = False
        for c in self.curves + self.fft_curves:
            c.setData([], [])
        self.readout.setText("cursors: no data")
        self.meas.setText("")

    # drawing (30 Hz timer)
    def redraw(self) -> bool:
        if not self._dirty or self.frame is None:
            return False
        self._dirty = False
        f = self.frame
        n = f.samples.shape[1]
        fs = f.sample_rate_hz or None
        scale, unit = (1e3 / fs, "ms") if fs else (1.0, "sample")
        if (scale, unit) != (self._scale, self._xunit):
            for ln in self.cur:                       # keep cursors on the same samples
                ln.setValue(ln.value() / self._scale * scale)
            self._scale, self._xunit = scale, unit
            self.plot.setLabel("bottom", "time (ms)" if fs else "sample index (no trustworthy rate)")
            self.fft.setLabel("bottom", "frequency (Hz)" if fs else "frequency (cycles/sample)")
            self.plot.setXRange(0, n * scale, padding=0)
        x = np.arange(n) * scale
        self.head.setRegion((0, f.head_skip * scale))
        ordered = bool(f.waves and f.waves[0].time_ordered)
        if ordered:                                 # hardware trigger at index 512
            self.trig_mark.setValue(512 * scale)
            self.trig_mark.show()
        else:
            self.trig_mark.hide()
        shown = set()
        meas = []
        self.peaks = {}
        for row, ch in enumerate(f.channels):
            y = f.samples[row]
            self.curves[ch].setData(x, y)
            shown.add(ch)
            body = y[f.head_skip:]
            freqs, db, pk = fft_peak(body, fs)
            if freqs is not None:
                self.fft_curves[ch].setData(freqs, db)
            else:
                self.fft_curves[ch].setData([], [])
            per = median_period(body)
            fper = (fs / per) if (per and fs) else None
            self.peaks[ch] = (pk, fper)
            lo, hi = int(body.min()), int(body.max())
            txt = f"CH{ch + 1}: {lo}..{hi} counts (span {hi - lo})"
            if pk is not None:
                txt += f"  FFT peak {fmt_hz(pk) if fs else f'{pk:.5f} cyc/sample'}"
            if per:
                txt += f"  period {per:.3f} samples" + (f" = {fmt_hz(fper)}" if fper else "")
                if not ordered:     # the record may hold a seam: the median filter drops cut periods
                    txt += " (seam-filtered)"
            meas.append(txt)
        for ch in (0, 1):
            if ch not in shown:
                self.curves[ch].setData([], [])
                self.fft_curves[ch].setData([], [])
        self.meas.setText("\n".join(meas))
        self._update_readout()
        self.draws.tick()
        return True

    def _update_readout(self, *_):
        f = self.frame
        if f is None:
            return
        n = f.samples.shape[1]
        idx = [int(round(ln.value() / self._scale)) for ln in self.cur]
        fs = f.sample_rate_hz
        di = idx[1] - idx[0]
        parts = []
        if fs:
            dt = di / fs
            parts.append(f"Δt {fmt_time(dt)}" + (f" (1/Δt {fmt_hz(1 / abs(dt))})" if di else ""))
        else:
            parts.append(f"Δ {di} samples (no trustworthy rate)")
        for row, ch in enumerate(f.channels):
            if all(0 <= i < n for i in idx):
                a, b = int(f.samples[row][idx[0]]), int(f.samples[row][idx[1]])
                parts.append(f"CH{ch + 1} A {a} B {b} Δ {b - a:+d} counts")
            else:
                parts.append(f"CH{ch + 1} cursor outside the record")
        self.readout.setText("cursors A/B: " + "   ".join(parts))


# ── meter ─────────────────────────────────────────────────────────────
# Stage-275: "function" (the function name from description section 5) is the
# last column, so readers of the first columns still work.
CSV_HEADER = ["t_unix", "update_count", "value", "unit", "display", "raw_bcd", "decimal_pos",
              "result", "submode", "ac", "autorange", "hold", "function"]
_PREFIX = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "k": 1e3, "M": 1e6}
_BASES = ("V", "A", "Ohm", "F")
_DECIMALS = re.compile(r"\.(\d+)")


def unit_scale(unit: str) -> Tuple[str, float]:
    """('mV') -> ('V', 1e-3): the base unit and the factor to it. A unit with
    no known prefix is its own base ('C' and 'F' for temperature too)."""
    if len(unit) > 1 and unit[0] in _PREFIX and unit[1:] in _BASES:
        return unit[1:], _PREFIX[unit[0]]
    return unit, 1.0


def display_decimals(display: str) -> int:
    """Digits after the point in the unit's display text ('3.254' -> 3)."""
    m = _DECIMALS.search(display or "")
    return len(m.group(1)) if m else 0


def free_log_path(path: str) -> str:
    """`path` if it is new, empty or has this header; else the first
    `<name>-N<ext>` (N = 2, 3, ...) that is. Stage-275: rows are never
    appended under an older header."""
    def usable(p: str) -> bool:
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return True
        with open(p, newline="") as f:
            return next(csv.reader(f), []) == CSV_HEADER
    if usable(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while not usable(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


class MeterModel:
    """The meter data, apart from its widget (stage-260): history, the last
    reading and its time, the blank state, and the CSV log.

    Every received reading is logged, whatever the display shows. The log
    appends: `start_log()` writes the header only to an empty file, so a
    restart with the same name keeps the earlier rows (review Q-8). A blank
    period (NAK NOT_READY) adds no CSV row; the --events log has its start and
    end.

    Stage-275 host tools. They never send a command; the log and the chart
    keep the readings as measured:
      * Hold freezes the shown reading (`shown_sample()`). It is not the
        unit's own HOLD flag;
      * Relative shows each reading minus the reading at the click;
      * minimum, maximum and average of the normal readings since the reset.
    A value in another unit of the same base (mV and V, kOhm and MOhm) is
    scaled, so an automatic range change keeps Relative and the statistics. A
    function change (another `submode`) ends all three and starts a new chart;
    a unit of another base ends Relative and the statistics. `tools_note` says
    why."""
    HISTORY = 1200

    def __init__(self):
        self.t0: Optional[float] = None
        self.ts: deque = deque(maxlen=self.HISTORY)
        self.vs: deque = deque(maxlen=self.HISTORY)
        self.unit = ""
        self.last: Optional[MeterSample] = None
        self.last_t: Optional[float] = None     # time.time() of the last reading
        self.blank = False
        self.log_file = None
        self.log_writer = None
        self.log_path: Optional[str] = None
        self.logged = 0
        self.names: Dict[int, str] = {}         # submode -> function name (section 5)
        self.units: Dict[int, str] = {}         # submode -> the function's unit (section 5)
        self.hold = False
        self.held: Optional[MeterSample] = None
        self.rel_ref: Optional[float] = None    # in the base unit
        self.rel_base = ""
        self.tools_note = ""                    # why the host tools ended at the last reading
        self.reset_stats()

    # ── readings ──────────────────────────────────────────────────
    def add(self, s: MeterSample) -> bool:
        """Store and log one reading. True if the chart got a point."""
        m = s.reading
        prev = self.last
        self.tools_note = ""
        if prev is not None and m.submode != prev.reading.submode:
            self.end_tools("the meter function changed")
            self.clear_chart()                  # also when the unit stays (DC V -> Diode)
        self.last, self.last_t, self.blank = s, s.t_unix, False
        point = m.result == "normal"
        if point:
            if m.unit != self.unit:
                if self.unit:
                    self.clear_chart()          # a unit change starts a new chart
                self.unit = m.unit
            if self.t0 is None:
                self.t0 = s.t_unix
            self.ts.append(s.t_unix - self.t0)
            self.vs.append(m.value)
            base, k = unit_scale(m.unit)
            if self.relative and base != self.rel_base:
                self.set_relative(False)
                self.tools_note = f"the unit changed to {labels.unit_words(m.unit)}"
            if self.n and base != self.stats_base:
                self.reset_stats()
                self.tools_note = f"the unit changed to {labels.unit_words(m.unit)}"
            v = m.value * k
            self.stats_base = base
            self.n += 1
            self.vsum += v
            self.vmin = v if self.vmin is None else min(self.vmin, v)
            self.vmax = v if self.vmax is None else max(self.vmax, v)
        if self.log_writer:
            self.log_writer.writerow([f"{s.t_unix:.3f}", m.update_count, m.value, m.unit,
                                      m.display, m.raw_bcd, m.decimal_pos, m.result, m.submode,
                                      int(m.ac), int(m.autorange), int(m.hold),
                                      self.names.get(m.submode, "")])
            self.log_file.flush()
            self.logged += 1
        return point

    def set_names(self, desc) -> None:
        """Function names from the description (None or no section 5: none)."""
        fns = getattr(desc, "meter_functions", ()) if desc is not None else ()
        self.names = {f.id: f.name for f in fns}
        self.units = {f.id: f.unit for f in fns}

    def function_name(self, submode: Optional[int] = None) -> Optional[str]:
        """The name of `submode` (default: the last reading's), or None."""
        if submode is None:
            if self.last is None:
                return None
            submode = self.last.reading.submode
        return self.names.get(submode)

    # ── host tools (stage-275) ────────────────────────────────────
    def set_hold(self, on: bool) -> bool:
        """Freeze the shown reading at the last one. False (and no hold) when
        there is no reading yet."""
        if on and self.last is None:
            on = False
        self.hold, self.held = on, (self.last if on else None)
        return on

    def set_relative(self, on: bool) -> bool:
        """Zero at the last reading. False (and off) when the last reading has
        no value (none yet, overload, blank)."""
        m = self.last.reading if self.last is not None else None
        if on and m is not None and m.result == "normal":
            self.rel_base, k = unit_scale(m.unit)
            self.rel_ref = m.value * k
            return True
        self.rel_ref, self.rel_base = None, ""
        return False

    @property
    def relative(self) -> bool:
        return self.rel_ref is not None

    def reset_stats(self) -> None:
        self.n = 0
        self.vsum = 0.0
        self.vmin: Optional[float] = None       # in the base unit
        self.vmax: Optional[float] = None
        self.stats_base = ""

    def end_tools(self, why: str) -> None:
        """Hold, Relative and the statistics end (a function change)."""
        if self.hold or self.relative or self.n:
            self.tools_note = why
        self.set_hold(False)
        self.set_relative(False)
        self.reset_stats()

    def stats(self, unit: Optional[str] = None) -> Optional[Tuple[float, float, float, int]]:
        """(minimum, maximum, average, count) in `unit` (default: the last
        normal reading's unit), or None with no normal reading since the reset."""
        if not self.n:
            return None
        base, k = unit_scale(self.unit if unit is None else unit)
        if base != self.stats_base:
            k = 1.0
        return self.vmin / k, self.vmax / k, self.vsum / self.n / k, self.n

    def rel_value(self, unit: Optional[str] = None) -> Optional[float]:
        """The Relative zero in `unit` (default: the last normal reading's)."""
        if not self.relative:
            return None
        base, k = unit_scale(self.unit if unit is None else unit)
        return self.rel_ref / (k if base == self.rel_base else 1.0)

    def shown_sample(self) -> Optional[MeterSample]:
        return self.held if self.hold else self.last

    def shown_text(self) -> str:
        """The big value: the shown reading, relative when Relative is on."""
        s = self.shown_sample()
        if s is None:
            return "—"
        m = s.reading
        if self.relative and m.result == "normal":
            base, k = unit_scale(m.unit)
            if base == self.rel_base:
                return labels.fmt_meter((m.value * k - self.rel_ref) / k,
                                        display_decimals(m.display), m.unit, signed=True)
        if not m.display.strip():
            return "—"
        return f"{m.display} {labels.unit_symbol(m.unit)}".strip()

    # ── state ─────────────────────────────────────────────────────
    def set_blank(self, on: bool) -> None:
        self.blank = on

    def age(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the last reading (None: no reading)."""
        if self.last_t is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self.last_t)

    def clear_chart(self) -> None:
        self.ts.clear()
        self.vs.clear()
        self.t0 = None

    def clear(self) -> None:
        """No unit: no value, no chart, no age, no host tools. The log stays open."""
        self.clear_chart()
        self.end_tools("")
        self.unit = ""
        self.last = self.last_t = None
        self.blank = False

    def start_log(self, path: str) -> str:
        """Open the log; returns the path used (another one when `path` has
        older columns: free_log_path)."""
        self.stop_log()
        path = free_log_path(path)
        self.log_file = open(path, "a", newline="")
        self.log_writer = csv.writer(self.log_file)
        self.log_path = path
        if os.fstat(self.log_file.fileno()).st_size == 0:
            self.log_writer.writerow(CSV_HEADER)
            self.log_file.flush()
        self.logged = 0
        return path

    def stop_log(self) -> None:
        if self.log_file:
            self.log_file.close()
        self.log_file = self.log_writer = None


def age_words(age: Optional[float]) -> str:
    if age is None:
        return "no reading yet"
    return f"last reading {age:.0f} s ago"


class MeterView(QtWidgets.QWidget):
    """The meter widget: draws from its MeterModel. During a blank period the
    value is grey and the line under it gives its age (review Q-7).

    Stage-275: the line under the value gives the function and the unit in
    words; the line under that tells when Hold or Relative is on. The flags
    are in words (no unit key names)."""
    HISTORY = MeterModel.HISTORY
    LIVE_STYLE = ""
    BLANK_STYLE = "color:#8a8a8a"

    def __init__(self, parent=None, model: Optional[MeterModel] = None):
        super().__init__(parent)
        self.model = model if model is not None else MeterModel()
        self.updates = Rate(5.0)
        self._dirty = False
        self._age_text = ""

        self.big = QtWidgets.QLabel("—")
        fnt = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        fnt.setPointSize(36)
        self.big.setFont(fnt)
        self.big.setAlignment(QtCore.Qt.AlignCenter)
        self.func = QtWidgets.QLabel("")
        self.func.setAlignment(QtCore.Qt.AlignCenter)
        fb = self.func.font()
        fb.setPointSize(fb.pointSize() + 3)
        self.func.setFont(fb)
        self.tools = QtWidgets.QLabel("")
        self.tools.setAlignment(QtCore.Qt.AlignCenter)
        self.tools.setStyleSheet("color:#2d9cdb; font-weight:bold")
        self.info = QtWidgets.QLabel("")
        self.info.setAlignment(QtCore.Qt.AlignCenter)
        self.age = QtWidgets.QLabel("")
        self.age.setAlignment(QtCore.Qt.AlignCenter)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "time (s)")
        self.plot.setLabel("left", "reading")
        self.curve = self.plot.plot([], [], pen=pg.mkPen("#6fcf97", width=1.5),
                                    symbol="o", symbolSize=3, symbolBrush="#6fcf97")
        self.log_btn = QtWidgets.QPushButton("Log CSV…")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(self._toggle_log)
        self.clear_btn = QtWidgets.QPushButton("Clear chart")
        self.clear_btn.clicked.connect(self.clear_chart)
        self.log_label = QtWidgets.QLabel("not logging")
        row = QtWidgets.QHBoxLayout()
        for w in (self.log_btn, self.clear_btn, self.log_label):
            row.addWidget(w)
        row.addStretch(1)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.big)
        lay.addWidget(self.func)
        lay.addWidget(self.tools)
        lay.addWidget(self.info)
        lay.addWidget(self.age)
        lay.addWidget(self.plot, 1)
        lay.addLayout(row)

    # the model's data, under the names the window and the tests use
    @property
    def ts(self) -> deque:
        return self.model.ts

    @property
    def vs(self) -> deque:
        return self.model.vs

    @property
    def log_file(self):
        return self.model.log_file

    @property
    def blank(self) -> bool:
        return self.model.blank

    def add(self, s: MeterSample) -> None:
        m = s.reading
        self.updates.tick()
        unit = self.model.unit
        if self.model.add(s):
            self._dirty = True
        if self.model.unit != unit:
            self.plot.setLabel("left", f"reading ({labels.unit_symbol(self.model.unit)})")
        self.big.setStyleSheet(self.LIVE_STYLE)
        flags = labels.meter_flags_words(m.ac, m.autorange, m.hold)
        self.info.setText(labels.SEP.join([f"reading {m.update_count}", m.result] + flags
                                          + [f"value {m.value:g} {labels.unit_symbol(m.unit)}"]))
        self.show_value()
        self._show_age()
        if self.model.log_writer:
            self.log_label.setText(f"logging: {self.model.logged} rows → {self.model.log_path}")

    def show_value(self) -> None:
        """The value, the function line and the tools line from the model
        (after a reading, and after a Hold or Relative click)."""
        mm = self.model
        self.big.setText(mm.shown_text())
        s = mm.shown_sample()
        if s is not None:                       # an overload reading has no unit: the function's
            r = s.reading
            self.func.setText(labels.function_line(mm.function_name(r.submode),
                                                   r.unit or mm.units.get(r.submode, "")))
        else:
            self.func.setText("")
        tools = []
        if mm.hold:
            tools.append("Hold: value frozen (logging continues)")
        if mm.relative:
            dec = display_decimals(mm.last.reading.display) if mm.last is not None else 3
            unit = mm.unit if unit_scale(mm.unit)[0] == mm.rel_base else mm.rel_base
            tools.append(f"Relative to {labels.fmt_meter(mm.rel_value(unit), dec, unit)}")
        self.tools.setText(labels.SEP.join(tools))

    def set_blank(self, on: bool) -> None:
        """The worker's meter_blank (on) / meter_ok (off) event."""
        self.model.set_blank(on)
        self.big.setStyleSheet(self.BLANK_STYLE if on else self.LIVE_STYLE)
        self._show_age()

    def _show_age(self, now: Optional[float] = None) -> None:
        text = age_words(self.model.age(now)) if self.model.blank else ""
        if text != self._age_text:              # the label changes about once a second
            self._age_text = text
            self.age.setText(text)

    def redraw(self) -> bool:
        if self.model.blank:
            self._show_age()
        if not self._dirty:
            return False
        self._dirty = False
        self.curve.setData(list(self.model.ts), list(self.model.vs))
        return True

    def clear_chart(self) -> None:
        self.model.clear_chart()
        self.curve.setData([], [])

    def clear(self) -> None:
        """Disconnect: empty the value, the chart and the age."""
        self.model.clear()
        self.big.setText("—")
        self.big.setStyleSheet(self.LIVE_STYLE)
        self.func.setText("")
        self.tools.setText("")
        self.info.setText("")
        self._show_age()
        self.plot.setLabel("left", "reading")
        self.curve.setData([], [])
        self._dirty = False

    def start_log(self, path: str) -> str:
        used = self.model.start_log(path)
        if used != path:
            self.log_label.setText(f"logging → {used} ({os.path.basename(path)} has older columns)")
        else:
            self.log_label.setText(f"logging → {used}")
        return used

    def stop_log(self) -> None:
        f = self.model.log_file
        self.model.stop_log()
        if f:
            self.log_label.setText(f"stopped: {self.model.logged} rows in {f.name}")

    def _toggle_log(self, on: bool) -> None:
        if on:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Meter log", time.strftime("meter_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
            if not path:
                self.log_btn.setChecked(False)
                return
            self.start_log(path)
            self.log_btn.setText("Stop log")
        else:
            self.stop_log()
            self.log_btn.setText("Log CSV…")
