"""Scope and meter views (pyqtgraph). They only draw: no port access here.

Honesty rules carried over from the library: samples are raw ADC counts
(no per-unit calibration, no zero point), so the vertical axis says so; time
and frequency are shown only when the frame carries a sample rate, otherwise
the axis is in samples. Samples [0, head_skip) are the record-head defect and
are shaded and left out of every measurement.
"""
from __future__ import annotations

import csv
import time
from collections import deque
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .worker import MeterSample, ScopeFrame

CH_COLORS = ("#f2c94c", "#56ccf2")      # CH1 yellow, CH2 cyan (the unit's own colours)
FFT_PAD = 8192
MIN_SPAN = 4        # counts: below this a record is flat/noise (proto.estimate_period's rule)


# ── analysis helpers (pure numpy; unit-testable) ──────────────────────
def fft_peak(x: np.ndarray, fs: Optional[float]):
    """Spectrum of one record body and its strongest non-DC peak.
    Returns (freqs, mag_db, peak_freq). Hann window, zero-padded to FFT_PAD,
    parabolic interpolation on the log magnitude around the peak bin.
    Frequencies in Hz when fs is known, else in cycles/sample."""
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
    if 0 < k < len(spec) - 1:
        a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
    else:
        d = 0.0
    return freqs, db, (k + d) * unit / FFT_PAD


def median_period(x: np.ndarray) -> Optional[float]:
    """Mean of the single periods (rising midpoint crossings, sub-sample)
    within 10 % of their median: a seam that cuts a period cannot bias it
    (G-240; same method as openscope-bench/capture_stats.py). In samples."""
    x = np.asarray(x, float)
    if len(x) < 8:
        return None
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    if hi - lo < MIN_SPAN:
        return None
    mid = (lo + hi) / 2
    b = x > mid
    r = np.nonzero(~b[:-1] & b[1:])[0]
    if len(r) < 3:
        return None
    rr = r + (mid - x[r]) / (x[r + 1] - x[r])
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
        self.t.append(time.monotonic() if now is None else now)

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
CSV_HEADER = ["t_unix", "update_count", "value", "unit", "display", "raw_bcd", "decimal_pos",
              "result", "submode", "ac", "autorange", "hold"]


class MeterView(QtWidgets.QWidget):
    HISTORY = 1200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.t0: Optional[float] = None
        self.ts: deque = deque(maxlen=self.HISTORY)
        self.vs: deque = deque(maxlen=self.HISTORY)
        self.unit = ""
        self.log_file = None
        self.log_writer = None
        self.logged = 0
        self.updates = Rate(5.0)
        self._dirty = False

        self.big = QtWidgets.QLabel("—")
        fnt = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        fnt.setPointSize(36)
        self.big.setFont(fnt)
        self.big.setAlignment(QtCore.Qt.AlignCenter)
        self.info = QtWidgets.QLabel("")
        self.info.setAlignment(QtCore.Qt.AlignCenter)
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
        lay.addWidget(self.info)
        lay.addWidget(self.plot, 1)
        lay.addLayout(row)

    def add(self, s: MeterSample) -> None:
        m = s.reading
        self.updates.tick()
        valid = m.result == "normal"
        self.big.setText(f"{m.display} {m.unit}".strip() if m.display.strip() else "—")
        flags = [x for x, on in (("AC", m.ac), ("AUTO", m.autorange), ("HOLD", m.hold)) if on]
        self.info.setText(f"#{m.update_count}  {m.result}  {' '.join(flags)}  "
                          f"value {m.value:g} {m.unit}")
        if valid:
            if m.unit != self.unit:
                if self.unit:
                    self.clear_chart()          # a unit change starts a new chart
                self.unit = m.unit
                self.plot.setLabel("left", f"reading ({m.unit})")
            if self.t0 is None:
                self.t0 = s.t_unix
            self.ts.append(s.t_unix - self.t0)
            self.vs.append(m.value)
            self._dirty = True
        if self.log_writer:
            self.log_writer.writerow([f"{s.t_unix:.3f}", m.update_count, m.value, m.unit,
                                      m.display, m.raw_bcd, m.decimal_pos, m.result, m.submode,
                                      int(m.ac), int(m.autorange), int(m.hold)])
            self.log_file.flush()
            self.logged += 1
            self.log_label.setText(f"logging: {self.logged} rows → {self.log_file.name}")

    def redraw(self) -> bool:
        if not self._dirty:
            return False
        self._dirty = False
        self.curve.setData(list(self.ts), list(self.vs))
        return True

    def clear_chart(self) -> None:
        self.ts.clear()
        self.vs.clear()
        self.t0 = None
        self.curve.setData([], [])

    def start_log(self, path: str) -> None:
        self.stop_log()
        self.log_file = open(path, "w", newline="")
        self.log_writer = csv.writer(self.log_file)
        self.log_writer.writerow(CSV_HEADER)
        self.logged = 0
        self.log_label.setText(f"logging → {path}")

    def stop_log(self) -> None:
        if self.log_file:
            self.log_file.close()
            self.log_label.setText(f"stopped: {self.logged} rows in {self.log_file.name}")
        self.log_file = self.log_writer = None

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
