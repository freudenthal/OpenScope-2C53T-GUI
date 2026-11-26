"""gui/views.py: the analysis helpers, the cursor readout and the meter CSV log,
on synthetic data with an offscreen Qt platform (no window shown).
Needs the `gui` extra; skipped without it."""
from __future__ import annotations

import csv
import importlib.util
import os
import sys
import tempfile
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)
from openscope import proto  # noqa: E402

# Stage-180: only a missing PySide6/numpy/pyqtgraph skips; an import error
# inside openscope_gui fails the suite.
HAVE_QT = all(importlib.util.find_spec(m) is not None for m in ("PySide6", "numpy", "pyqtgraph"))
if HAVE_QT:
    import numpy as np
    from PySide6 import QtWidgets
    from openscope_gui import views  # noqa: E402
    from openscope_gui.worker import MeterSample, ScopeFrame  # noqa: E402

FS = 12490


def square(f_hz, n=1024, fs=FS, lo=12, hi=100, seam_at=None):
    t = np.arange(n) / fs
    # finite edges (like the input stage), so crossings interpolate between samples
    x = np.round((hi + lo) / 2 + (hi - lo) / 2 * np.tanh(3 * np.sin(2 * np.pi * f_hz * t))).astype(np.uint8)
    if seam_at is not None:          # a seam: the record jumps, cutting the period it lands in
        x = np.concatenate([x[:seam_at], x[seam_at + 6:], x[-6:]])
        x = np.concatenate([x[:seam_at + 250], x[seam_at + 254:], x[-4:]])
    return x


def frame(samples, rate=FS, head_skip=128, channels=(0,), ordered=False):
    rows = np.atleast_2d(samples).astype(np.uint8)
    flags = (proto.WAVE_FLAG_TB_MEASURED if rate else 0) | (proto.WAVE_FLAG_TIME_ORDERED if ordered else 0)
    waves = tuple(proto.Waveform(7, ch, flags, 0x10, 5,
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

    # stage-255 (Q-9): small noisy sines, a ramp, a peak between bins
    def noisy_sine(self, counts, period=37.3, n=896, seed=1):
        rng = np.random.default_rng(seed)
        i = np.arange(n)
        x = 128 + counts / 2 * np.sin(2 * np.pi * i / period) + rng.integers(-1, 2, n)
        return np.round(x).astype(np.uint8)

    def test_median_period_on_small_noisy_sines(self):
        for counts in (10, 20):
            for seed in range(5):
                x = self.noisy_sine(counts, seed=seed)
                per = views.median_period(x)
                self.assertIsNotNone(per, f"{counts} counts seed {seed}")
                self.assertAlmostEqual(per / 37.3, 1.0, delta=0.01, msg=f"{counts} counts seed {seed}")
        # negative control: without hysteresis (the stage-250 GUI) 1 LSB noise
        # adds crossings and the median period of a 10-count sine is far off
        x = self.noisy_sine(10).astype(float)
        mid = (np.percentile(x, 5) + np.percentile(x, 95)) / 2
        b = x > mid
        r = np.nonzero(~b[:-1] & b[1:])[0]
        self.assertLess(np.median(np.diff(r)), 0.9 * 37.3)

    def test_ramp_has_no_fft_peak(self):
        for x in (np.linspace(10, 200, 896), np.r_[np.full(448, 20.0), np.linspace(20, 200, 448)]):
            freqs, db, pk = views.fft_peak(np.round(x).astype(np.uint8), FS)
            self.assertIsNotNone(freqs)                 # the spectrum is still drawn
            self.assertIsNone(pk)
        # negative control: the stage-250 step at k = lo reported a "peak" 3 bins off
        x = np.linspace(10, 200, 896)
        spec = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(896), views.FFT_PAD))
        lo = int(np.ceil(2 * views.FFT_PAD / 896))
        k = lo + int(np.argmax(spec[lo:]))
        a, b, c = np.log(spec[k - 1:k + 2])
        self.assertEqual(k, lo)
        self.assertGreater(abs(0.5 * (a - c) / (a - 2 * b + c)), 0.5)

    def test_one_edge_of_a_slow_square_has_no_fft_peak(self):
        # stage-255 bench: 10 Hz on CH2 at 0x10 (12.5 kS/s) - the 896-sample body holds
        # less than a period; the old rule showed a "35 Hz" peak (a leakage sidelobe)
        hits = 0
        for phase in np.linspace(0, 0.1, 21):
            t = phase + np.arange(1024) / 12500
            x = np.round(128 + 40 * np.tanh(3 * np.sin(2 * np.pi * 10 * t))).astype(np.uint8)[128:]
            if np.ptp(x) < 4:
                continue
            hits += 1
            self.assertIsNone(views.fft_peak(x, 12500)[2], f"phase {phase:.3f}")
        self.assertGreater(hits, 10)
        # a tone of 3 periods in the body, just above the low cut, is still found
        f = 3 * 12500 / 896
        x = np.round(128 + 60 * np.sin(2 * np.pi * f * np.arange(896) / 12500)).astype(np.uint8)
        self.assertAlmostEqual(views.fft_peak(x, 12500)[2], f, delta=0.02 * f)

    def test_fft_peak_between_bins_stays_within_half_a_bin(self):
        binw = FS / views.FFT_PAD
        for f in np.arange(400.0, 420.0, 0.37 * binw):
            i = np.arange(896)
            x = np.round(128 + 60 * np.sin(2 * np.pi * f * i / FS)).astype(np.uint8)
            _, _, pk = views.fft_peak(x, FS)
            self.assertLess(abs(pk - f), 0.5 * binw, f"{f:.3f} Hz")

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

    def test_period_says_seam_filtered_unless_time_ordered(self):
        v = views.ScopeView()
        v.set_frame(frame(square(1000.0, seam_at=411)))
        v.redraw()
        self.assertIn("(seam-filtered)", v.meas.text())
        self.assertAlmostEqual(v.peaks[0][1], 1000.0, delta=2.0)
        self.assertFalse(v.trig_mark.isVisible())
        v.set_frame(frame(square(1000.0), ordered=True))
        v.redraw()
        self.assertNotIn("seam-filtered", v.meas.text())
        self.assertIn("period", v.meas.text())

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


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestMeterStage260(unittest.TestCase):
    """MeterModel apart from MeterView; Q-7 (blank shows grey with its age) and
    Q-8 (the log appends under one header)."""
    setUpClass = TestViews.setUpClass

    @staticmethod
    def sample(uc, t, val=3.254):
        return MeterSample(t, proto.MeterReading(uc, val, 0, 3, 1, 0, 0, 0, "V", f"{val}"))

    def test_q8_two_logs_on_one_file_append_under_one_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            for run in range(2):                        # two launches with --meter-log m.csv
                m = views.MeterView()
                m.start_log(path)
                for i in range(3):
                    m.add(self.sample(run * 10 + i, 1000.0 + run * 10 + i))
                m.stop_log()
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0], views.CSV_HEADER)
        self.assertEqual(sum(1 for r in rows if r == views.CSV_HEADER), 1)
        self.assertEqual([r[1] for r in rows[1:]], ["0", "1", "2", "10", "11", "12"])

    def test_q7_blank_period_is_grey_with_its_age_then_live_again(self):
        m = views.MeterView()
        now = 1000.0
        m.add(self.sample(1, now))
        self.assertEqual(m.big.styleSheet(), views.MeterView.LIVE_STYLE)
        m.set_blank(True)                               # the worker's meter_blank event
        self.assertEqual(m.big.styleSheet(), views.MeterView.BLANK_STYLE)
        self.assertEqual(m.big.text(), "3.254 V")       # the last value stays, in grey
        m._show_age(now + 4.2)
        self.assertEqual(m.age.text(), "last reading 4 s ago")
        self.assertAlmostEqual(m.model.age(now + 4.2), 4.2)
        m.add(self.sample(2, now + 5))                  # a new reading: normal colour, no age
        self.assertFalse(m.blank)
        self.assertEqual(m.big.styleSheet(), views.MeterView.LIVE_STYLE)
        self.assertEqual(m.age.text(), "")

    def test_blank_before_any_reading_says_so(self):
        m = views.MeterView()
        m.set_blank(True)
        self.assertEqual(m.age.text(), "no reading yet")
        m.set_blank(False)                              # meter_ok
        self.assertEqual((m.age.text(), m.big.styleSheet()), ("", views.MeterView.LIVE_STYLE))

    def test_clear_empties_value_chart_and_age(self):
        m = views.MeterView()
        m.add(self.sample(1, 1000.0))
        m.set_blank(True)
        m.clear()
        self.assertEqual((m.big.text(), m.info.text(), m.age.text()), ("—", "", ""))
        self.assertEqual((list(m.ts), list(m.vs), m.model.last, m.blank), ([], [], None, False))
        self.assertEqual(m.big.styleSheet(), views.MeterView.LIVE_STYLE)

    def test_model_works_without_a_widget(self):
        mm = views.MeterModel()
        self.assertTrue(mm.add(self.sample(1, 5.0)))
        self.assertFalse(mm.add(MeterSample(6.0, proto.MeterReading(2, 0.0, 0, 3, 6, 0, 0, 0, "V", ""))))
        self.assertEqual((list(mm.vs), mm.last.reading.update_count, mm.last_t), ([3.254], 2, 6.0))


@unittest.skipUnless(HAVE_QT, "PySide6/numpy/pyqtgraph not installed (pip install -e .[gui])")
class TestMeterStage275(unittest.TestCase):
    """Host tools on the MeterModel, words in the MeterView, the function CSV
    column (stage-275)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @staticmethod
    def s(uc, val, unit="V", disp=None, sub=0, cls=1, flags=0):
        disp = f"{val:.3f}" if disp is None else disp
        return MeterSample(1000.0 + uc, proto.MeterReading(uc, val, 0, 3, cls, flags, sub, 0, unit, disp))

    @staticmethod
    def names(*fns):
        return types.SimpleNamespace(meter_functions=tuple(proto.MeterFunction(i, True, n, u)
                                                           for i, n, u in fns))

    @staticmethod
    def rows(path):
        with open(path, newline="") as f:
            return list(csv.reader(f))

    def test_hold_freezes_the_shown_value_while_the_model_logs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            v = views.MeterView()
            self.assertFalse(v.model.set_hold(True))            # nothing to hold yet
            v.start_log(path)
            v.add(self.s(1, 1.0))
            self.assertTrue(v.model.set_hold(True))
            v.show_value()
            for uc, val in ((2, 2.0), (3, 3.0), (4, 4.0)):
                v.add(self.s(uc, val))
            self.assertEqual(v.big.text(), "1.000 V")
            self.assertIn("Hold", v.tools.text())
            self.assertEqual(list(v.vs), [1.0, 2.0, 3.0, 4.0])  # the chart keeps every reading
            v.stop_log()
            self.assertEqual([r[2] for r in self.rows(path)[1:]], ["1.0", "2.0", "3.0", "4.0"])
            v.model.set_hold(False)
            v.show_value()
            self.assertEqual((v.big.text(), v.tools.text()), ("4.000 V", ""))

    def test_relative_subtracts_and_the_log_keeps_the_raw_reading(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            mm = views.MeterModel()
            self.assertFalse(mm.set_relative(True))             # no reading yet
            mm.start_log(path)
            mm.add(self.s(1, 3.254))
            self.assertTrue(mm.set_relative(True))
            self.assertEqual(mm.shown_text(), "+0.000 V")
            mm.add(self.s(2, 3.300))
            self.assertEqual(mm.shown_text(), "+0.046 V")
            mm.add(self.s(3, 3.200))
            self.assertEqual(mm.shown_text(), "-0.054 V")
            mm.add(self.s(4, 0.0, disp="OL", cls=3))            # no value: shown as the unit shows it
            self.assertEqual(mm.shown_text(), "OL V")
            mm.stop_log()
            self.assertEqual([r[2] for r in self.rows(path)[1:]], ["3.254", "3.3", "3.2", "0.0"])
            mm.set_relative(False)
            self.assertFalse(mm.set_relative(True))             # the last reading has no value

    def test_an_automatic_range_change_keeps_relative_and_the_statistics(self):
        mm = views.MeterModel()
        mm.add(self.s(1, 1.5))
        mm.set_relative(True)
        mm.add(self.s(2, 500.0, "mV", "500.0"))
        self.assertTrue(mm.relative)
        self.assertEqual(mm.shown_text(), "-1000.0 mV")
        self.assertEqual(mm.tools_note, "")
        lo, hi, avg, n = mm.stats("V")
        self.assertEqual(n, 2)
        for got, want in ((lo, 0.5), (hi, 1.5), (avg, 1.0)):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(mm.stats()[1], 1500.0)           # default: the last reading's unit

    def test_min_max_average_over_a_known_series_and_reset(self):
        mm = views.MeterModel()
        self.assertIsNone(mm.stats())
        for uc, val in enumerate((2.0, 5.0, 3.0, 6.0), start=1):
            mm.add(self.s(uc, val))
        mm.add(self.s(9, 0.0, disp="OL", cls=3))                # overrange: not a value
        lo, hi, avg, n = mm.stats()
        self.assertEqual((lo, hi, n), (2.0, 6.0, 4))
        self.assertAlmostEqual(avg, 4.0)
        mm.reset_stats()
        self.assertIsNone(mm.stats())
        mm.add(self.s(10, 7.0))
        self.assertEqual(mm.stats(), (7.0, 7.0, 7.0, 1))

    def test_a_function_change_ends_hold_relative_and_statistics(self):
        mm = views.MeterModel()
        mm.add(self.s(1, 1.0))
        mm.set_hold(True)
        mm.set_relative(True)
        mm.add(self.s(2, 4.7, "kOhm", sub=6))
        self.assertEqual((mm.hold, mm.relative, mm.stats()[3]), (False, False, 1))
        self.assertEqual(mm.tools_note, "the meter function changed")
        self.assertEqual(mm.shown_text(), "4.700 kΩ")
        mm.add(self.s(3, 4.8, "kOhm", sub=6))
        self.assertEqual(mm.tools_note, "")

    def test_a_function_change_starts_a_new_chart_also_with_the_same_unit(self):
        mm = views.MeterModel()
        mm.add(self.s(1, 3.255))
        mm.add(self.s(2, 0.620, sub=8))                         # DC V -> Diode: both volts
        self.assertEqual(list(mm.vs), [0.620])

    def test_an_overload_reading_names_the_unit_of_its_function(self):
        v = views.MeterView()
        v.model.set_names(self.names((6, "Resistance", "kOhm")))
        v.add(self.s(1, 0.0, "", "OL", sub=6, cls=3))           # bench: OL comes with no unit
        self.assertEqual((v.big.text(), v.func.text()), ("OL", "Resistance · kilohms"))

    def test_a_unit_of_another_kind_ends_relative_and_statistics(self):
        mm = views.MeterModel()
        mm.add(self.s(1, 24.8, "C", "24.8", sub=10))
        mm.set_relative(True)
        mm.add(self.s(2, 76.6, "F", "76.6", sub=10))
        self.assertFalse(mm.relative)
        self.assertEqual(mm.stats()[3], 1)
        self.assertIn("degrees Fahrenheit", mm.tools_note)

    def test_csv_has_the_function_name_as_the_last_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            mm = views.MeterModel()
            mm.set_names(self.names((0, "DC Voltage", "V")))
            mm.start_log(path)
            mm.add(self.s(1, 1.0))
            mm.add(self.s(2, 4.7, "kOhm", sub=6))               # not listed: empty name
            mm.stop_log()
            rows = self.rows(path)
            self.assertEqual(rows[0], views.CSV_HEADER)
            self.assertEqual(rows[0][:12], ["t_unix", "update_count", "value", "unit", "display",
                                            "raw_bcd", "decimal_pos", "result", "submode", "ac",
                                            "autorange", "hold"])      # stage-260 columns first
            self.assertEqual((rows[0][-1], rows[1][-1], rows[2][-1]), ("function", "DC Voltage", ""))
            self.assertEqual(len(rows[1]), len(views.CSV_HEADER))

    def test_an_older_header_is_not_appended_to(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.csv")
            with open(path, "w", newline="") as f:
                csv.writer(f).writerows([views.CSV_HEADER[:-1], ["1.0"] * 12])
            for run in range(2):                                # the second run appends to -2
                v = views.MeterView()
                used = v.start_log(path)
                self.assertEqual(used, os.path.join(d, "m-2.csv"))
                self.assertIn("older columns", v.log_label.text())
                v.add(self.s(run + 1, 1.0))
                v.stop_log()
            self.assertEqual(len(self.rows(path)), 2)           # the old file is unchanged
            new = self.rows(used)
            self.assertEqual((new[0], len(new)), (views.CSV_HEADER, 3))

    def test_function_unit_and_flags_in_words(self):
        v = views.MeterView()
        v.model.set_names(self.names((6, "Resistance", "kOhm")))
        v.add(self.s(1, 4.7, "kOhm", sub=6, flags=0x04 | 0x08))
        self.assertEqual(v.big.text(), "4.700 kΩ")
        self.assertEqual(v.func.text(), "Resistance · kilohms")
        self.assertIn("automatic range", v.info.text())
        self.assertIn("held on the unit", v.info.text())
        self.assertNotRegex(v.info.text(), r"\b(AUTO|HOLD)\b|#")
        v.model.set_names(None)                                 # firmware without section 5
        v.add(self.s(2, 4.7, "kOhm", sub=6))
        self.assertEqual(v.func.text(), "kilohms")


if __name__ == "__main__":
    unittest.main()
