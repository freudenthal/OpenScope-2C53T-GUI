"""gui/worker.py against a fake Device: no hardware, no window.

Needs PySide6 and numpy (the `gui` extra); skipped without them, like the
MCP suite without the mcp SDK."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openscope import proto  # noqa: E402
from openscope.device import DeviceError, Nak, Screen, Timeout  # noqa: E402
from openscope.link import NoDevice  # noqa: E402

try:
    from PySide6 import QtCore
    import numpy as np
    from openscope.gui.worker import ScopeFrame, Worker  # noqa: E402
    HAVE_QT = True
except ImportError:          # pragma: no cover - depends on the venv
    HAVE_QT = False


def status(mode=0, flags=proto.FLAG_CAPTURE_READY):
    return proto.Status(1, mode, 90, flags, 4100, 1000, 0, 0, "fake")


def wave(fid, ch=0, n=1024, rate=12490, flags=proto.WAVE_FLAG_TB_MEASURED):
    samples = bytes((i * 7 + ch * 50) & 0xFF for i in range(n))
    return proto.Waveform(fid, ch, flags, 0x10, 5, n, 24, rate, 0, 32, 128, samples)


def reading(uc, value=3.254):
    return proto.MeterReading(uc, value, 3254, 3, 1, 0x04, 0, 0, "V", "3.254")


class FakeLink:
    port = "COMFAKE"


class FakeDevice:
    """Scripted: each queue holds return values or exceptions, consumed in order;
    the last entry repeats."""

    def __init__(self, mode=0, waves=None, meters=None, statuses=None):
        self.link = FakeLink()
        self.statuses = statuses or [status(mode)]
        self.waves = waves if waves is not None else [[wave(1)]]
        self.meters = meters or [reading(1)]
        self.calls = []
        self.closed = False
        self.screen = Screen(0, 0, 4, 2, bytes([0x01, 0x23, 0x45, 0x67]))

    @staticmethod
    def _next(q):
        item = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def status(self):
        self.calls.append("status")
        return self._next(self.statuses)

    def waveform(self, mask=1):
        self.calls.append(("waveform", mask))
        out = self._next(self.waves)
        return [w for w in out if mask & (1 << w.channel)]

    def meter(self):
        self.calls.append("meter")
        return self._next(self.meters)

    def press(self, b):
        self.calls.append(("press", b))

    def screenshot(self):
        self.calls.append("screenshot")
        return self.screen

    def close(self):
        self.closed = True


class Opener:
    """Returns the scripted devices / raises the scripted errors, in order."""

    def __init__(self, *items):
        self.items = list(items)
        self.count = 0

    def __call__(self):
        self.count += 1
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


@unittest.skipUnless(HAVE_QT, "PySide6/numpy not installed (pip install -e .[gui])")
class TestWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def make(self, opener, **kw):
        kw.setdefault("status_s", 0.0)
        w = Worker(opener, retry_s=0.0, idle_s=0.0, meter_s=0.0, nak_s=0.0, **kw)
        self.ev = {"connection": [], "status": [], "frame": [], "meter": [], "note": [], "job": []}
        w.connection.connect(lambda s, d: self.ev["connection"].append((s, d)))
        w.status.connect(lambda st: self.ev["status"].append(st))
        w.frame.connect(lambda f: self.ev["frame"].append(f))
        w.meter.connect(lambda m: self.ev["meter"].append(m))
        w.note.connect(lambda n: self.ev["note"].append(n))
        w.job_done.connect(lambda j, ok, msg: self.ev["job"].append((j, ok, msg)))
        return w

    def states(self):
        return [s for s, _ in self.ev["connection"]]

    def test_connects_and_emits_only_new_frames(self):
        dev = FakeDevice(waves=[[wave(1)], [wave(1)], [wave(2)], [wave(2)], [wave(3)]])
        w = self.make(Opener(dev))
        for _ in range(5):
            w.step()
        self.assertEqual(self.states(), ["connected"])
        self.assertEqual(self.ev["connection"][0][1], "COMFAKE")
        self.assertEqual([f.frame_id for f in self.ev["frame"]], [1, 2, 3])
        f = self.ev["frame"][0]
        self.assertIsInstance(f, ScopeFrame)
        self.assertEqual(f.samples.shape, (1, 1024))
        self.assertEqual(f.samples.dtype, np.uint8)
        self.assertEqual((f.sample_rate_hz, f.head_skip, f.timebase_tier), (12490, 128, "measured"))
        self.assertEqual(w.stats["frames"], 3)

    def test_two_channels_in_row_order(self):
        dev = FakeDevice(waves=[[wave(5, 0), wave(5, 1)]])
        w = self.make(Opener(dev))
        w.set_channels(3)
        w.step()
        f = self.ev["frame"][0]
        self.assertEqual(f.channels, (0, 1))
        self.assertEqual(f.samples.shape, (2, 1024))
        self.assertIn(("waveform", 3), dev.calls)
        with self.assertRaises(ValueError):
            w.set_channels(0)

    def test_nak_is_a_note_not_a_disconnect(self):
        no_data = Nak(proto.CMD_GET_WAVEFORM, 0x0B)
        dev = FakeDevice(waves=[no_data, no_data, [wave(9)]])
        w = self.make(Opener(dev))
        for _ in range(3):
            w.step()
        self.assertEqual(self.ev["note"], ["NO_CAPTURE_DATA", "NO_CAPTURE_DATA"])
        self.assertEqual([f.frame_id for f in self.ev["frame"]], [9])
        self.assertEqual(self.states(), ["connected"])
        self.assertFalse(dev.closed)

    def test_meter_mode_new_readings_only_and_not_ready_tolerated(self):
        nr = Nak(proto.CMD_GET_METER, 0x07)
        dev = FakeDevice(mode=1, meters=[reading(1), reading(1), nr, reading(2)])
        w = self.make(Opener(dev))
        for _ in range(4):
            w.step()
        self.assertEqual([m.reading.update_count for m in self.ev["meter"]], [1, 2])
        self.assertEqual(self.ev["note"], ["NOT_READY"])
        self.assertNotIn(("waveform", 1), dev.calls)       # no scope polls in meter mode

    def test_other_modes_poll_status_only(self):
        dev = FakeDevice(mode=3)
        w = self.make(Opener(dev))
        w.step()
        w.step()
        self.assertEqual(dev.calls, ["status", "status"])

    def test_status_is_rate_limited(self):
        t = [0.0]
        dev = FakeDevice(waves=[[wave(i)] for i in range(1, 10)])
        w = self.make(Opener(dev), status_s=0.5, clock=lambda: t[0])
        for _ in range(4):
            w.step()
            t[0] += 0.2
        self.assertEqual(dev.calls.count("status"), 2)     # t = 0 and t = 0.6

    def test_loss_then_search_then_recover(self):
        dev1 = FakeDevice(waves=[[wave(1)], DeviceError("port lost twice")])
        dev2 = FakeDevice(waves=[[wave(50)]])
        w = self.make(Opener(dev1, NoDevice("no OpenScope found"), NoDevice("no OpenScope found"), dev2))
        for _ in range(6):
            w.step()
        self.assertEqual(self.states(), ["connected", "disconnected", "searching", "connected"])
        self.assertTrue(dev1.closed)
        self.assertEqual([f.frame_id for f in self.ev["frame"]], [1, 50])
        self.assertEqual((w.stats["connects"], w.stats["losses"]), (2, 1))

    def test_failed_reopen_nodevice_counts_as_loss(self):
        dev = FakeDevice(waves=[NoDevice("device did not come back within 6 s")])
        w = self.make(Opener(dev, NoDevice("x")))
        w.step()
        self.assertEqual(self.states(), ["connected", "disconnected"])
        self.assertIsNone(w.dev)

    def test_timeouts_drop_only_after_three_in_a_row(self):
        to = Timeout("no reply")
        dev = FakeDevice(waves=[to, to, [wave(1)], to, to, to])
        w = self.make(Opener(dev, NoDevice("x")))
        for _ in range(3):
            w.step()
        self.assertEqual(self.states(), ["connected"])
        for _ in range(3):
            w.step()
        self.assertEqual(self.states(), ["connected", "disconnected"])

    def test_press_job_runs_in_order_and_forces_a_status_poll(self):
        t = [0.0]
        dev = FakeDevice()
        w = self.make(Opener(dev), status_s=10.0, clock=lambda: t[0])
        w.step()
        w.press("menu")
        w.step()
        self.assertEqual(self.ev["job"], [("press", True, "MENU")])
        i = dev.calls.index(("press", 9))
        self.assertEqual(dev.calls[i + 1], "status")
        with self.assertRaises(ValueError):
            w.press("NOPE")

    def test_jobs_fail_cleanly_when_not_connected(self):
        w = self.make(Opener(NoDevice("no OpenScope found")))
        w.press("OK")
        w.step()
        self.assertEqual(self.ev["job"], [("press", False, "not connected")])
        self.assertEqual(self.states(), ["searching"])

    def test_screenshot_job_writes_png(self):
        dev = FakeDevice()
        w = self.make(Opener(dev))
        w.step()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.png")
            w.screenshot(path)
            w.step()
            with open(path, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        job, ok, msg = self.ev["job"][0]
        self.assertEqual((job, ok), ("screenshot", True))
        self.assertIn("CRC ok", msg)

    def test_screenshot_failure_is_reported_not_a_disconnect(self):
        dev = FakeDevice()

        def bad():
            raise DeviceError("screenshot failed after 3 attempts")
        dev.screenshot = bad
        w = self.make(Opener(dev))
        w.step()
        w.screenshot("unused.png")
        w.step()
        self.assertFalse(self.ev["job"][0][1])
        self.assertEqual(self.states(), ["connected"])

    def test_thread_delivers_frames_and_stops(self):
        """The real QThread: signals cross to this thread's event loop."""
        dev = FakeDevice(waves=[[wave(i)] for i in range(1, 400)])
        w = self.make(Opener(dev))
        loop = QtCore.QEventLoop()
        w.frame.connect(lambda f: loop.quit() if len(self.ev["frame"]) >= 20 else None)
        QtCore.QTimer.singleShot(5000, loop.quit)
        t0 = time.monotonic()
        w.start()
        loop.exec()
        self.assertTrue(w.stop(3000), "worker did not stop")
        self.assertGreaterEqual(len(self.ev["frame"]), 20)
        self.assertLess(time.monotonic() - t0, 5.0)
        ids = [f.frame_id for f in self.ev["frame"]]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(dev.closed)


if __name__ == "__main__":
    unittest.main()
