"""gui/views.py: the analysis helpers, the cursor readout and the meter CSV log,
on synthetic data with an offscreen Qt platform (no window shown).
Needs the `gui` extra; skipped without it."""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)
from openscope import proto  # noqa: E402

try:
    import numpy as np
    from PySide6 import QtWidgets
    from openscope_gui import views  # noqa: E402
    from openscope_gui.worker import MeterSample, ScopeFrame  # noqa: E402
    HAVE_QT = True
except ImportError:          # pragma: no cover - depends on the venv
    HAVE_QT = False

FS = 12490


def square(f_hz, n=1024, fs=FS, lo=12, hi=100, seam_at=None):
    t = np.arange(n) / fs
    # finite edges (like the input stage), so crossings interpolate between samples
    x = np.round((hi + lo) / 2 + (hi - lo) / 2 * np.tanh(3 * np.sin(2 * np.pi * f_hz * t))).astype(np.uint8)
    if seam_at is not None:          # a seam: the record jumps, cutting the period it lands in
        x = np.concatenate([x[:seam_at], x[seam_at + 6:], x[-6:]])
        x = np.concatenate([x[:seam_at + 250], x[seam_at + 254:], x[-4:]])
    return x


def frame(samples, rate=FS, head_skip=128, channels=(0,)):
    rows = np.atleast_2d(samples).astype(np.uint8)
    waves = tuple(proto.Waveform(7, ch, proto.WAVE_FLAG_TB_MEASURED if rate else 0, 0x10, 5,
                                 rows.shape[1], 24, rate, 0, 32, head_skip, rows[i].tobytes())
                  for i, ch in enumerate(channels))
    return ScopeFrame.from_waves(waves, 0.0)


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestAnalysis(unittest.TestCase):
    def test_fft_peak_of_a_1khz_square(self):
        _, _, pk = views.fft_peak(square(1000.0)[128:], FS)
        self.assertAlmostEqual(pk, 1000.0, delta=5.0)          # 0.5 %

    def test_fft_peak_survives_a_seam(self):
        _, _, pk = views.fft_peak(square(1000.0, seam_at=411)[128:], FS)
        self.assertAlmostEqual(pk, 1000.0, delta=20.0)         # the 2 % acceptance bound

    def test_fft_without_rate_is_in_cycles_per_sample(self):
        _, _, pk = views.fft_peak(square(1000.0)[128:], None)
        self.assertAlmostEqual(pk, 1000.0 / FS, delta=0.0005)

    def test_flat_record_has_no_peak_or_period(self):
        flat = np.full(900, 156, np.uint8)
        flat[::7] = 158                                         # open input: span 2 counts
        self.assertEqual(views.fft_peak(flat, FS), (None, None, None))
        self.assertIsNone(views.median_period(flat))

    def test_median_period_ignores_seam_cut_periods(self):
        x = square(1000.0, seam_at=411)[128:]
        per = views.median_period(x)
        self.assertAlmostEqual(FS / per, 1000.0, delta=2.0)    # 0.2 %
        # negative control: the plain mean over all crossings is what the seams bias
        b = x > 56
        r = np.nonzero(~b[:-1] & b[1:])[0]
        self.assertGreater(abs(FS / np.diff(r).mean() - 1000.0), 5.0)

    def test_rate_window(self):
        r = views.Rate(2.0)
        for i in range(11):
            r.tick(i * 0.1)
        self.assertAlmostEqual(r.value(1.0), 10.0, places=6)
        self.assertEqual(r.value(10.0), 0.0)

    def test_rate_stays_bounded_without_value(self):
        # code review 2026-11-23 Q-2: the window ticks every 33 ms and reads value()
        # only with --stats; tick() alone must drop old entries.
        r = views.Rate(2.0)
        for i in range(10000):
            r.tick(i * 0.04)                    # 25 per second for 400 s
        self.assertLessEqual(len(r.t), 2.0 * 25 + 2)
        self.assertAlmostEqual(r.value(9999 * 0.04), 25.0, places=6)


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if not isinstance(cls.app, QtWidgets.QApplication):
            raise unittest.SkipTest("a QCoreApplication already exists in this process")

    def test_redraw_only_on_new_frame(self):
        v = views.ScopeView()
        self.assertFalse(v.redraw())
        v.set_frame(frame(square(1000.0)))
        self.assertTrue(v.redraw())
        self.assertFalse(v.redraw())

    def test_cursor_delta_t_and_delta_counts(self):
        x = np.arange(1024) % 256
        v = views.ScopeView()
        v.set_frame(frame(x))
        v.redraw()
        ms = 1e3 / FS
        v.cur[0].setValue(100 * ms)
        v.cur[1].setValue(250 * ms)
        txt = v.readout.text()
        self.assertIn(f"Δt {150 / FS * 1e3:.4g} ms", txt)
        self.assertIn("CH1 A 100 B 250 Δ +150 counts", txt)
        self.assertIn(f"1/Δt {FS / 150:.2f} Hz", txt)

    def test_cursors_stay_on_their_samples_when_the_rate_goes_away(self):
        x = np.arange(1024) % 256
        v = views.ScopeView()
        v.set_frame(frame(x))
        v.redraw()
        v.cur[0].setValue(100 * 1e3 / FS)
        v.cur[1].setValue(250 * 1e3 / FS)
        v.set_frame(frame(x, rate=0))
        v.redraw()
        self.assertAlmostEqual(v.cur[0].value(), 100.0, places=6)
        txt = v.readout.text()
        self.assertIn("Δ 150 samples (no trustworthy rate)", txt)
        self.assertIn("CH1 A 100 B 250", txt)

    def test_two_channels_and_peaks(self):
        v = views.ScopeView()
        v.set_frame(frame(np.vstack([square(1000.0), np.full(1024, 156)]), channels=(0, 1)))
        v.redraw()
        self.assertAlmostEqual(v.peaks[0][0], 1000.0, delta=5.0)
        self.assertAlmostEqual(v.peaks[0][1], 1000.0, delta=2.0)
        self.assertEqual(v.peaks[1], (None, None))
        self.assertEqual(len(v.curves[1].getData()[0]), 1024)
        v.set_frame(frame(square(1000.0)))                    # back to CH1 only
        v.redraw()
        self.assertIsNone(v.curves[1].getData()[0])

    def test_meter_chart_and_csv_log(self):
        m = views.MeterView()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            m.start_log(path)
            for i, (val, unit, cls) in enumerate([(3.254, "V", 1), (3.255, "V", 1),
                                                   (0.0, "V", 6), (1.2, "Ohm", 1)]):
                r = proto.MeterReading(i + 1, val, 0, 3, cls, 0, 0, 0, unit, f"{val}")
                m.add(MeterSample(1000.0 + i, r))
            m.stop_log()
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0], views.CSV_HEADER)
        self.assertEqual(len(rows), 5)                         # blank readings are logged too
        self.assertEqual(rows[3][7], "blank")
        self.assertEqual(list(m.vs), [1.2])                    # unit change cleared the chart
        self.assertTrue(m.redraw())


if __name__ == "__main__":
    unittest.main()
