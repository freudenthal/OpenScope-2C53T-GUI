"""gui/worker.py against a fake Device: no hardware, no window.

Needs PySide6 and numpy (the `gui` extra); skipped without them, like the
MCP suite without the mcp SDK. Stage-180: only a MISSING PySide6/numpy skips;
an import error inside openscope_gui fails the suite."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E402,F401  (this repo and the openscope library)
from openscope import proto  # noqa: E402
from openscope.device import DeviceError, Nak, Screen, Timeout  # noqa: E402
from openscope.link import NoDevice  # noqa: E402

HAVE_QT = all(importlib.util.find_spec(m) is not None for m in ("PySide6", "numpy"))
if HAVE_QT:                  # an openscope_gui import error must fail, not skip
    from PySide6 import QtCore
    import numpy as np
    from openscope_gui.worker import ScopeFrame, Worker, link_lost  # noqa: E402
    from openscope_gui import worker as worker_mod  # noqa: E402


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

    def screenshot(self, cancel=None):
        self.calls.append("screenshot")
        self.cancel = cancel
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
        self.assertEqual(self.ev["note"], [worker_mod.METER_BLANK_NOTE])     # stage-260: blank-meter note
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


class DescDevice(FakeDevice):
    """FakeDevice with the stage-086/087 methods."""

    def __init__(self, desc=None, **kw):
        super().__init__(**kw)
        self.desc = desc
        self.shell_lines = []

    def description(self):
        self.calls.append("description")
        if isinstance(self.desc, BaseException):
            raise self.desc
        return self.desc

    def set_probe(self, ch, factor):
        self._set("set_probe", ch, factor, probe=(factor, 1) if ch == 1 else (10, factor))

    def ping(self):
        return "fake fw"

    def shell(self, line, timeout=3.0):
        self.shell_lines.append(line)
        return f"out of {line}"


@unittest.skipUnless(HAVE_QT, "PySide6/numpy not installed (pip install -e .[gui])")
class TestWorkerStage087(unittest.TestCase):
    setUpClass = TestWorker.setUpClass
    states = TestWorker.states

    def setUp(self):
        self.d = proto.Description(1, 32, ((12490, 2),), 32, (((42950, 2),),), None, (10, 1))

    def test_description_once_per_connection(self):
        dev = DescDevice(self.d)
        w = self.make(Opener(dev))
        for _ in range(3):
            w.step()
        self.assertEqual(dev.calls.count("description"), 1)
        self.assertEqual(self.ev_desc, [self.d])

    def test_old_firmware_description_is_none_and_noted(self):
        dev = DescDevice(Nak(proto.CMD_GET_DESCRIPTION, 0x01))
        w = self.make(Opener(dev))
        w.step()
        w.step()
        self.assertEqual(self.ev_desc, [None])
        self.assertTrue(any("no device description" in n for n in self.ev["note"]))
        self.assertEqual(self.states(), ["connected"])

    def test_probe_set_then_state(self):
        dev = DescDevice(self.d)
        w = self.make(Opener(dev))
        w.step()
        w.set_probe(1, 1)
        w.step()
        self.assertIn(("set_probe", 1, 1), dev.calls)
        self.assertEqual(self.ev["state"][-1].probe, (1, 1))
        self.assertIn(("probe", True), self.ev_set)

    def test_failed_set_emits_set_done_false(self):
        dev = DescDevice(self.d)
        dev.set_error = Nak(proto.CMD_SET_MODE, 0x05)
        w = self.make(Opener(dev))
        w.step()
        w.set("mode", "meter")
        w.step()
        self.assertEqual(self.ev_set, [("mode", False)])

    def test_offline_drops_and_fails_jobs(self):
        dev = DescDevice(self.d)
        w = self.make(Opener(dev))
        w.step()
        w.set_online(False)
        w.set("run", False)
        w.step()
        self.assertTrue(dev.closed)
        self.assertEqual(self.states(), ["connected", "offline"])
        self.assertEqual(self.ev_set, [("run", False)])
        w.set_online(True)
        w.step()
        self.assertEqual(self.states()[-1], "connected")

    def test_set_port_reconnects_with_the_port(self):
        got = []
        dev = DescDevice(self.d)

        def opener(port=None):
            got.append(port)
            return dev
        w = self.make(opener)
        w.step()
        w.set_port("COM9")
        w.step()
        w.step()
        self.assertEqual(got, [None, "COM9"])

    def test_info_job(self):
        dev = DescDevice(self.d)
        w = self.make(Opener(dev))
        w.step()
        got = []
        w.info.connect(got.append)
        w.request_info()
        w.step()
        self.assertEqual(got[0]["version"], "fake fw")
        self.assertEqual(got[0]["description"], self.d)
        self.assertIsNotNone(got[0]["state"])

    def test_shell_job_and_i40_refusal(self):
        dev = DescDevice(self.d)
        w = self.make(Opener(dev))
        out = []
        w.shell_out.connect(lambda c, t: out.append((c, t)))
        w.step()
        w.shell("version")
        with self.assertRaises(ValueError):
            w.shell("mem write 0x20000000 0")
        with self.assertRaises(ValueError):
            w.shell("cal restore")
        w.step()
        self.assertEqual(dev.shell_lines, ["version"])
        self.assertEqual(out, [("version", "out of version")])

    def make(self, opener, **kw):
        w = TestWorker.make(self, opener, **kw)
        self.ev_desc, self.ev_set = [], []
        w.description.connect(self.ev_desc.append)
        w.set_done.connect(lambda what, ok: self.ev_set.append((what, ok)))
        return w


@unittest.skipUnless(HAVE_QT, "PySide6/numpy not installed (pip install -e .[gui])")
class TestWorkerStage180(unittest.TestCase):
    """Review Q-3, Q-5, Q-6 and the busy port (stage-180)."""
    setUpClass = TestWorker.setUpClass
    setUp = TestWorkerStage087.setUp
    states = TestWorker.states

    def make(self, opener, **kw):
        w = TestWorkerStage087.make(self, opener, **kw)
        self.ev_shell = []
        w.shell_out.connect(lambda c, t: self.ev_shell.append((c, t)))
        return w

    def test_q3_set_channels_writes_only_the_mask(self):
        dev = FakeDevice(waves=[[wave(5, 0), wave(5, 1)]])
        w = self.make(Opener(dev))
        w.step()                                    # frame 5, CH1
        before = w._last_key
        w.set_channels(3)                           # from the GUI thread
        self.assertEqual(w._last_key, before, "the GUI thread wrote worker-thread state")
        w.step()                                    # same frame 5, new mask: a repaint
        w.step()                                    # same frame, same mask: nothing
        self.assertEqual([(f.frame_id, f.channels) for f in self.ev["frame"]], [(5, (0,)), (5, (0, 1))])

    def test_q3_mask_is_read_once_per_poll(self):
        dev = FakeDevice(waves=[[wave(7, 0), wave(7, 1)]])
        w = self.make(Opener(dev))
        real = dev.waveform

        def waveform(mask=1):
            w.mask = 3 if mask == 1 else 1          # the GUI changes it during the request
            return real(mask)
        dev.waveform = waveform
        w.step()
        self.assertEqual(w._last_key, (7, 1), "dedupe key must hold the mask that was asked for")

    def test_q5_non_ascii_shell_line_reports_and_keeps_the_link(self):
        dev = DescDevice()

        def shell(line, timeout=3.0):
            line.encode("ascii")                    # what Device._shell_once does
            return "ok"
        dev.shell = shell
        w = self.make(Opener(dev))
        w.step()
        w.shell("ä")
        w.step()
        job, ok, msg = self.ev["job"][-1]
        self.assertEqual((job, ok), ("shell", False))
        self.assertIn("UnicodeEncodeError", msg)
        self.assertTrue(self.ev_shell[-1][1].startswith("error:"))
        self.assertEqual(self.states(), ["connected"])
        self.assertFalse(dev.closed)

    def test_q5_unexpected_exception_still_ends_the_job(self):
        dev = FakeDevice()

        def press(b):
            raise RuntimeError("bug in a library path")
        dev.press = press
        w = self.make(Opener(dev))
        w.step()
        w.press("OK")
        with tempfile.TemporaryDirectory() as d:
            w.screenshot(os.path.join(d, "s.png"))  # the next job still runs
            w.step()
        self.assertEqual([(j, ok) for j, ok, _ in self.ev["job"]], [("press", False), ("screenshot", True)])
        self.assertEqual(self.states(), ["connected"])

    def test_q5_link_loss_in_a_job_reports_then_drops(self):
        for err in (NoDevice("port lost"), OSError(22, "The device does not recognize the command"),
                    DeviceError("port lost after 0x09 was sent; not re-sending")):
            self.ev = None
            dev = FakeDevice()
            dev.screen = err
            w = self.make(Opener(dev, NoDevice("x")))
            w.step()
            w.screenshot("unused.png")
            w.press("OK")
            w.step()
            self.assertEqual(self.ev["job"][0][:2], ("screenshot", False), err)
            self.assertEqual(self.states(), ["connected", "disconnected"], err)
            self.assertTrue(dev.closed)
            w.step()                                # the press queued behind it fails cleanly
            self.assertEqual(self.ev["job"][1], ("press", False, "not connected"))

    def test_link_lost_classes(self):
        self.assertTrue(link_lost(NoDevice("port lost")))
        self.assertTrue(link_lost(OSError(5, "x")))
        self.assertTrue(link_lost(DeviceError("version check after reconnect failed: x")))
        self.assertFalse(link_lost(DeviceError("screenshot failed after 3 attempts")))
        self.assertFalse(link_lost(Nak(proto.CMD_SET_MODE, 0x05)))
        self.assertFalse(link_lost(Timeout("port lost")))
        self.assertFalse(link_lost(ValueError("x")))

    def test_q6_bad_description_reply_is_none_noted_once_and_polling_goes_on(self):
        dev = DescDevice(proto.ProtocolError("DESCRIPTION: 3 B, need 8"),
                         waves=[[wave(1)], [wave(2)], [wave(3)]])
        w = self.make(Opener(dev))
        for _ in range(3):
            w.step()
        self.assertEqual(self.ev_desc, [None])
        self.assertEqual(dev.calls.count("description"), 1)
        self.assertEqual(len([n for n in self.ev["note"] if "no device description" in n]), 1)
        self.assertEqual([f.frame_id for f in self.ev["frame"]], [1, 2, 3])
        self.assertEqual(self.states(), ["connected"])

    def test_new_description_after_every_reconnect(self):
        d2 = proto.Description(1, 32, ((24979, 2),), 32, (((42950, 2),),), None, (1, 1))
        dev1 = DescDevice(self.d, waves=[[wave(1)], NoDevice("port lost")])
        dev2 = DescDevice(d2, waves=[[wave(9)]])
        w = self.make(Opener(dev1, dev2))
        for _ in range(4):
            w.step()
        self.assertEqual(self.states(), ["connected", "disconnected", "connected"])
        self.assertEqual(self.ev_desc[-1], d2)
        self.assertEqual(len(self.ev_desc), 2)

    def test_busy_port_is_searching_with_the_reason(self):
        busy = NoDevice("cannot open COM7: in use by another process (only one client at a time)")
        w = self.make(Opener(NoDevice("no OpenScope found"), busy, busy, NoDevice("no OpenScope found")))
        for _ in range(4):
            w.step()
        self.assertEqual(self.ev["connection"], [("searching", "no OpenScope found"),
                                                 ("searching", str(busy)),
                                                 ("searching", "no OpenScope found")])

    def test_screenshot_passes_a_cancel_that_follows_stop(self):
        dev = FakeDevice()
        w = self.make(Opener(dev))
        w.step()
        with tempfile.TemporaryDirectory() as d:
            w.screenshot(os.path.join(d, "s.png"))
            w.step()
        self.assertFalse(dev.cancel())
        w.stop()
        self.assertTrue(dev.cancel())


@unittest.skipUnless(HAVE_QT, "PySide6/numpy not installed (pip install -e .[gui])")
class TestWorkerStage260(unittest.TestCase):
    """Mode table, read-back kinds, capability flags, typed events, blank meter."""
    setUpClass = TestWorker.setUpClass
    states = TestWorker.states

    def make(self, opener, **kw):
        w = TestWorker.make(self, opener, **kw)
        self.events = []
        w.event.connect(lambda e: self.events.append(e))
        return w

    def kinds(self, *only):
        return [e.kind for e in self.events if not only or e.kind in only]

    def test_one_poller_entry_per_mode(self):
        self.assertEqual(set(worker_mod.POLLERS), {0, 1, 2})
        self.assertEqual((worker_mod.MODE_SCOPE, worker_mod.MODE_METER, worker_mod.MODE_SIGGEN),
                         (0, 1, 2))
        for mode, name in worker_mod.POLLERS.items():
            self.assertTrue(callable(getattr(Worker, name)), mode)

    def test_generator_mode_makes_no_meter_or_waveform_request(self):
        dev = FakeDevice(mode=2)
        w = self.make(Opener(dev))
        for _ in range(50):
            w.step()
        self.assertEqual(dev.calls.count("meter"), 0)
        self.assertEqual(sum(1 for c in dev.calls if c[0] == "waveform"), 0)
        self.assertEqual(dev.calls.count("status"), 50)     # status_s = 0: STATUS each step
        self.assertEqual(self.states(), ["connected"])

    def test_every_setter_has_a_method_and_a_known_read_back(self):
        for what, (method, kind) in worker_mod.SETTERS.items():
            self.assertIn(kind, worker_mod.READBACKS, what)
            self.assertTrue(method.startswith(("set_", "run_")), what)

    def test_a_set_runs_only_its_own_read_back(self):
        dev = FakeDevice()
        dev.set_meter_thing = lambda *a: dev.calls.append(("set_meter_thing",) + a)
        dev.set_plain = lambda *a: dev.calls.append(("set_plain",) + a)
        added = {"meter_thing": ("set_meter_thing", "meter"), "plain": ("set_plain", "none"),
                 "gen_thing": ("set_plain", "siggen")}
        worker_mod.SETTERS.update(added)
        try:
            w = self.make(Opener(dev), status_s=10.0)
            w.step()
            for what, readback in (("meter_thing", ["meter"]), ("plain", []), ("gen_thing", []),
                                   ("timebase", ["scope_state"])):
                start = len(dev.calls)
                w.set(what, 1)
                w.step()
                calls = dev.calls[start:]
                # the set, its read-back, then the scope mode poll (one waveform)
                self.assertEqual([c for c in calls[1:] if not (isinstance(c, tuple) and c[0] == "waveform")],
                                 readback, what)
        finally:
            for k in added:
                del worker_mod.SETTERS[k]

    def test_capability_flags_are_not_asked_after_a_reconnect(self):
        dev1 = FakeDevice(mode=1)
        dev1.state_error = Nak(proto.CMD_GET_SCOPE_STATE, 0x01)
        dev2 = FakeDevice(mode=3)
        w = self.make(Opener(dev1, dev2))
        self.assertEqual(w.caps, {"scope": None, "meter": None, "siggen": None})
        w.step()
        self.assertEqual(w.caps, {"scope": False, "meter": True, "siggen": None})
        self.assertFalse(w.has_state)
        dev1.statuses = [DeviceError("port lost")]
        w.step()                                            # loss
        self.assertEqual(self.states(), ["connected", "disconnected"])
        w._opener = Opener(dev2)
        w.step()                                            # new connect, then STATUS + SCOPE_STATE
        self.assertEqual(dev2.calls, ["status", "scope_state"])     # asked again: the flag was reset
        self.assertEqual(w.caps, {"scope": True, "meter": None, "siggen": None})
        self.assertTrue(w.has_state)
        with self.assertRaises(AttributeError):
            w.has_state = False                             # read-only alias

    def test_blank_meter_period_is_one_event_one_note_no_drop(self):
        nr = Nak(proto.CMD_GET_METER, 0x07)
        dev = FakeDevice(mode=1, meters=[reading(1), nr, nr, nr, nr, reading(2), reading(2),
                                         nr, reading(3)])
        w = self.make(Opener(dev))
        for _ in range(9):
            w.step()
        self.assertEqual(self.kinds("meter_blank", "meter_ok"),
                         ["meter_blank", "meter_ok", "meter_blank", "meter_ok"])
        self.assertEqual(self.ev["note"], [worker_mod.METER_BLANK_NOTE] * 2)  # one per blank period
        self.assertEqual([m.reading.update_count for m in self.ev["meter"]], [1, 2, 3])
        self.assertEqual(self.states(), ["connected"])
        self.assertFalse(dev.closed)
        self.assertNotIn("nak", self.kinds())               # a blank meter is not a NAK event

    def test_other_meter_naks_stay_naks(self):
        dev = FakeDevice(mode=1, meters=[Nak(proto.CMD_GET_METER, 0x09), reading(1)])
        w = self.make(Opener(dev))
        w.step()
        w.step()
        self.assertEqual(self.kinds("nak", "meter_blank"), ["nak"])
        self.assertEqual(self.events[0].data["request"], "GET_METER")

    def test_typed_events_next_to_the_text_signals(self):
        no_data = Nak(proto.CMD_GET_WAVEFORM, 0x0B)
        dev = FakeDevice(waves=[no_data, Timeout("no reply"), proto.ProtocolError("bad crc"),
                                [wave(1)]])
        w = self.make(Opener(dev))
        for _ in range(4):
            w.step()
        w.set("run", False)
        w.step()
        nak = next(e for e in self.events if e.kind == "nak")
        self.assertEqual((nak.ok, nak.text, nak.data),
                         (False, "NO_CAPTURE_DATA", {"error": "NO_CAPTURE_DATA", "request": "GET_WAVEFORM"}))
        self.assertEqual(self.kinds("nak", "timeout", "protocol"), ["nak", "timeout", "protocol"])
        job = next(e for e in self.events if e.kind == "job")
        self.assertEqual((job.ok, job.text, job.data), (True, "run False", {"job": "set"}))
        self.assertEqual(self.ev["job"], [("set", True, "run False")])     # job_done still comes
        self.assertEqual(len(self.ev["note"]), 3)                          # note still comes
        with self.assertRaises(Exception):
            nak.kind = "x"                                                 # frozen
        self.assertTrue(set(self.kinds()) <= set(worker_mod.EVENT_KINDS))


if __name__ == "__main__":
    unittest.main()
