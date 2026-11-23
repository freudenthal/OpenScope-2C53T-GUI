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


def scope_state(running=True, tb=0x10, mode=0, r1=6, r2=7, tmode=0, level=0):
    ch = (proto.ScopeChannel(True, r1, 2, 42950), proto.ScopeChannel(True, r2, 2, 88420))
    return proto.ScopeState(1, mode, running, tb, tb, 2, tmode, 0, 0, level, 12490, 0x80, 32, ch)


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
        self.state = scope_state()
        self.state_error = None             # e.g. Nak: firmware without the control surface
        self.set_error = None

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
        if isinstance(self.screen, BaseException):
            raise self.screen
        return self.screen

    def scope_state(self):
        self.calls.append("scope_state")
        if self.state_error is not None:
            raise self.state_error
        return self.state

    def _set(self, name, *args, **change):
        self.calls.append((name,) + args)
        if self.set_error is not None:
            raise self.set_error
        fields = {f: getattr(self.state, f) for f in self.state.__dataclass_fields__}
        fields.update(change)
        self.state = proto.ScopeState(**fields)

    def set_mode(self, m):
        self._set("set_mode", m, mode=proto.mode_id(m))

    def set_timebase(self, code):
        self._set("set_timebase", code, timebase_idx=code, timebase_code=code)

    def set_vdiv(self, ch, r):
        chans = list(self.state.ch)
        chans[ch - 1] = proto.ScopeChannel(True, r, 2, 1)
        self._set("set_vdiv", ch, r, ch=tuple(chans))

    def set_trigger(self, mode, edge="rising", source="CH1", level=0):
        self._set("set_trigger", mode, edge, source, level, trig_level=level)

    def run_stop(self, running):
        self._set("run_stop", running, running=running)

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
        self.ev = {"connection": [], "status": [], "frame": [], "meter": [], "note": [], "job": [],
                   "state": []}
        w.scope_state.connect(lambda s: self.ev["state"].append(s))
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

    def test_other_modes_poll_status_and_state_only(self):
        dev = FakeDevice(mode=3)
        w = self.make(Opener(dev))
        w.step()
        w.step()
        self.assertEqual(dev.calls, ["status", "scope_state"] * 2)
        self.assertEqual(len(self.ev["state"]), 2)

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

    # ── stage-085: scope sets, state, held screenshot ─────────────
    def test_each_set_calls_its_device_method_then_emits_the_state(self):
        dev = FakeDevice()
        w = self.make(Opener(dev), status_s=10.0)
        w.step()
        for what, args, call in (("timebase", (0x14,), ("set_timebase", 0x14)),
                                 ("vdiv", (2, 5), ("set_vdiv", 2, 5)),
                                 ("trigger", ("normal", "falling", "CH2", -30),
                                  ("set_trigger", "normal", "falling", "CH2", -30)),
                                 ("run", (False,), ("run_stop", False)),
                                 ("mode", ("meter",), ("set_mode", "meter"))):
            n = len(self.ev["state"])
            w.set(what, *args)
            w.step()
            i = dev.calls.index(call)
            self.assertEqual(dev.calls[i + 1], "scope_state", what)
            self.assertGreaterEqual(len(self.ev["state"]), n + 1, what)   # mode also re-polls STATUS
            self.assertEqual(self.ev["job"][-1][:2], ("set", True))
        s = self.ev["state"][-1]
        self.assertEqual((s.timebase_code, s.ch[1].vdiv_idx, s.trig_level, s.running, s.mode),
                         (0x14, 5, -30, False, 1))
        with self.assertRaises(ValueError):
            w.set("bandwidth", 1)

    def test_mode_set_forces_a_status_poll(self):
        t = [0.0]
        dev = FakeDevice()
        w = self.make(Opener(dev), status_s=10.0, clock=lambda: t[0])
        w.step()
        n = dev.calls.count("status")
        w.set("mode", "meter")
        w.step()
        self.assertEqual(dev.calls.count("status"), n + 1)

    def test_refused_set_is_a_failed_job_with_the_reason(self):
        dev = FakeDevice()
        w = self.make(Opener(dev))
        w.step()
        dev.set_error = Nak(proto.CMD_SET_TIMEBASE, 0x07)       # NOT_READY
        w.set("timebase", 0x12)
        w.step()
        job, ok, msg = self.ev["job"][-1]
        self.assertEqual((job, ok), ("set", False))
        self.assertIn("timebase refused: NOT_READY", msg)
        self.assertIn("nothing changed", msg)
        self.assertEqual(self.states(), ["connected"])

    def test_firmware_without_state_is_noted_once_and_not_polled(self):
        dev = FakeDevice(mode=3)
        dev.state_error = Nak(proto.CMD_GET_SCOPE_STATE, 0x01)  # UNKNOWN_CMD on an old image
        w = self.make(Opener(dev))
        for _ in range(3):
            w.step()
        self.assertEqual(dev.calls.count("scope_state"), 1)
        self.assertEqual(len(self.ev["note"]), 1)
        self.assertIn("no scope controls", self.ev["note"][0])
        self.assertEqual(self.states(), ["connected"])

    def _shot(self, dev, hold=True):
        w = self.make(Opener(dev), status_s=10.0)
        w.step()
        start = len(dev.calls)
        with tempfile.TemporaryDirectory() as d:
            w.screenshot(os.path.join(d, "s.png"), hold=hold)
            w.step()
        return dev.calls[start:], self.ev["job"][-1]

    def test_held_screenshot_stops_dumps_and_runs_again(self):
        calls, (job, ok, msg) = self._shot(FakeDevice())
        i = calls.index(("run_stop", False))
        self.assertEqual(calls[i:i + 3], [("run_stop", False), "screenshot", ("run_stop", True)])
        self.assertTrue(ok)
        self.assertIn("held", msg)

    def test_held_screenshot_leaves_a_stopped_scope_stopped(self):
        dev = FakeDevice()
        dev.state = scope_state(running=False)
        calls, (_, ok, msg) = self._shot(dev)
        self.assertNotIn(("run_stop", False), calls)
        self.assertNotIn(("run_stop", True), calls)
        self.assertTrue(ok)
        self.assertNotIn("held", msg)

    def test_held_screenshot_runs_again_when_the_dump_fails(self):
        dev = FakeDevice()
        dev.screen = DeviceError("screenshot failed after 3 attempts")
        calls, (job, ok, _) = self._shot(dev)
        i = calls.index("screenshot")
        self.assertEqual(calls[i + 1], ("run_stop", True))
        self.assertFalse(ok)

    def test_unheld_screenshot_does_not_touch_run_stop(self):
        calls, (_, ok, _) = self._shot(FakeDevice(), hold=False)
        self.assertEqual([c for c in calls if isinstance(c, tuple) and c[0] == "run_stop"], [])
        self.assertTrue(ok)

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
