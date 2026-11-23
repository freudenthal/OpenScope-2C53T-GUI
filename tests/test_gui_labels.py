"""gui/labels.py: practical-unit text from a real device description.

No Qt needed (labels.py is pure). The fixture is the raw DESCRIPTION payload
of bench unit #1 on guest-coldtrace-2910cef (2026-11-23, CH1 probe x10)."""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openscope import proto  # noqa: E402
from openscope.gui import labels  # noqa: E402

# GET_DESCRIPTION reply payload, 239 B, from the unit (stage-087 step 1).
REAL_DESCRIPTION = bytes.fromhex(
    "01016c002000150000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000fe30100010ac30000029361000002ca3000"
    "00027f13000002bf09000002e204000002f40100000202680020000a0200000000000000000000000000000000"
    "00000000335306000173ce090002404b1300024db8270002c05a7d00018e339e00010000000000000000000000"
    "0000000000000000005a43040001666a090002a6bc120002daa3250002a77e64000100ebbe0001030c009cff64"
    "00800001ce0001fee40402000a01")
HEX = re.compile(r"0x[0-9A-Fa-f]")


def desc():
    return proto.parse_description(REAL_DESCRIPTION)


def state(tb=0x10, r1=7, r2=7, probe=(10, 1), src=0, level=38, running=True, tier=2, rate=12490):
    ch = (proto.ScopeChannel(True, r1, 2, 26030850), proto.ScopeChannel(True, r2, 2, 2466778))
    return proto.ScopeState(1, 0, running, tb, tb, tier, 0, 0, src, level, rate, 0xAF, 32, ch, probe)


class TestFixture(unittest.TestCase):
    def test_real_description_parses(self):
        d = desc()
        self.assertEqual(len(REAL_DESCRIPTION), 239)
        self.assertEqual((d.samples_per_div, d.counts_per_div, d.probe), (32, 32, (10, 1)))
        self.assertEqual(d.sample_rate(0x10), 12490)
        self.assertIsNone(d.sample_rate(0x08))


class TestNumbers(unittest.TestCase):
    def test_time_rate_volts(self):
        self.assertEqual(labels.fmt_time_div(32 / 12490), "2.56 ms/div")
        self.assertEqual(labels.fmt_time_div(32 / 123663), "259 µs/div")
        self.assertEqual(labels.fmt_time_div(None), "timebase not calibrated")
        self.assertEqual(labels.fmt_rate(12490), "12.49 kS/s")
        self.assertEqual(labels.fmt_rate(500), "500 S/s")
        self.assertEqual(labels.fmt_rate(0), "no trusted sample rate")
        self.assertEqual(labels.fmt_volts_div(1.37), "1.37 V/div")
        self.assertEqual(labels.fmt_volts_div(0.04295), "43 mV/div")
        self.assertEqual(labels.fmt_volts(0.47, signed=True), "+470 mV")
        self.assertEqual(labels.fmt_volts(-1.2, signed=True), "-1.2 V")


class TestTimebaseItems(unittest.TestCase):
    def test_measured_provisional_and_hidden(self):
        items = labels.timebase_items(desc())
        self.assertEqual([i.value for i in items], list(range(0x0D, 0x15)))   # 0x00-0x0C hidden
        by = {i.value: i for i in items}
        self.assertEqual(by[0x10].text, "2.56 ms/div")
        self.assertEqual(by[0x0D].text, "≈ 259 µs/div")
        self.assertEqual(by[0x10].tooltip, "Timebase code 0x10, 12,490 samples/s, measured")

    def test_show_uncalibrated(self):
        items = labels.timebase_items(desc(), show_uncalibrated=True)
        self.assertEqual(len(items), 21)
        self.assertEqual(items[3].text, "Not calibrated (3)")
        self.assertIn("0x03", items[3].tooltip)

    def test_current_hidden_code_is_listed_without_code(self):
        items = labels.timebase_items(desc(), current=0x08)
        cur = [i for i in items if i.value == 0x08]
        self.assertEqual(cur[0].text, "Not calibrated")

    def test_no_description(self):
        self.assertEqual(labels.timebase_items(None), [])
        self.assertEqual([i.text for i in labels.timebase_items(None, current=5)], ["Not calibrated"])


class TestVdivItems(unittest.TestCase):
    def test_probe_included_and_sorted(self):
        items = labels.vdiv_items(desc(), 1, probe=10)
        self.assertEqual([i.value for i in items], [4, 5, 6, 7, 8, 9])         # 0-3 hidden
        by = {i.value: i for i in items}
        self.assertEqual(by[6].text, "12.6 V/div")
        self.assertEqual(by[4].text, "≈ 4.15 V/div")
        self.assertEqual(by[6].tooltip, "Range 6, 39.51 mV/count at the BNC, measured, probe ×10")
        x1 = {i.value: i.text for i in labels.vdiv_items(desc(), 1, probe=1)}
        self.assertEqual(x1[6], "1.26 V/div")

    def test_ch2_and_uncalibrated(self):
        items = labels.vdiv_items(desc(), 2, probe=1, show_uncalibrated=True)
        self.assertEqual(len(items), 10)
        self.assertEqual(items[-1].text, "Not calibrated (3)")     # no-volts ranges sort last


class TestNoCodesVisible(unittest.TestCase):
    def test_item_texts_have_no_hex(self):
        d = desc()
        texts = [i.text for i in labels.timebase_items(d, True)]
        for ch in (1, 2):
            texts += [i.text for i in labels.vdiv_items(d, ch, 10, True)]
        texts += labels.state_lines(state(), d)
        texts.append(labels.scope_status(state(), d))
        self.assertFalse([t for t in texts if HEX.search(t)])


class TestLevel(unittest.TestCase):
    def test_volts_on_source_channel_with_probe(self):
        ls = labels.level_scale(desc(), state(r1=7, probe=(10, 1)))
        self.assertEqual(ls.unit, "V")
        # 26.03 V/div / 32 counts/div * 256/206 counts per level step
        self.assertAlmostEqual(ls.step, 2603085 * 10 / 1e6 / 32 * 256 / 206, places=6)
        self.assertEqual(ls.level(ls.value(38)), 38)
        self.assertEqual(ls.level(1e9), 100)

    def test_percent_when_no_volts_meaning(self):
        ls = labels.level_scale(desc(), state(r1=2))
        self.assertEqual(ls.unit, "%")
        self.assertIn("% of screen", ls.text(10))

    def test_tooltip_has_the_register(self):
        self.assertIn("0xAF", labels.level_tooltip(state()))


class TestStateWords(unittest.TestCase):
    def test_lines(self):
        lines = labels.state_lines(state(), desc())
        self.assertEqual(lines[0], "Running · 2.56 ms/div · 12.49 kS/s (measured)")
        self.assertEqual(lines[1], "CH1 26 V/div (probe ×10, measured)")
        self.assertTrue(lines[3].startswith("Trigger Auto · Rising edge · CH1 · level +"))

    def test_single_in_words(self):
        s = proto.ScopeState(1, 0, True, 0x10, 0x10, 2, 2, 0, 0, 0, 12490, 0x80, 32, state().ch, (10, 1))
        self.assertTrue(labels.state_lines(s, desc())[0].startswith("Single capture (Run returns to continuous)"))
        self.assertTrue(labels.scope_status(s, desc()).startswith("Single capture · 2.56 ms/div"))
        self.assertEqual(labels.frame_notes(FakeFrame(), s, desc())[0], "single record")

    def test_stopped_and_uncalibrated(self):
        s = state(running=False, tb=0x08, tier=0, rate=0)
        line = labels.state_lines(s, desc())[0]
        self.assertEqual(line, "Stopped (held record) · timebase not calibrated · no trusted sample rate")


class FakeFrame:
    def __init__(self, tb=0x10, r1=7, rate=12490, tier="measured"):
        self.timebase_idx, self.sample_rate_hz, self.timebase_tier = tb, rate, tier
        self.waves = (proto.Waveform(1, 0, proto.WAVE_FLAG_TB_MEASURED, tb, r1, 1024, 24, rate,
                                     26030850 if r1 == 7 else 12600000, 32, 24, bytes(1024)),)


class TestFrameNotes(unittest.TestCase):
    def test_held_record_in_words(self):
        f = FakeFrame(tb=0x0F, r1=6)
        notes = labels.frame_notes(f, state(running=False, tb=0x10, r1=7), desc())
        self.assertEqual(notes[0], "held record")
        self.assertIn("record taken at 1.28 ms/div (now 2.56 ms/div)", notes[1])
        self.assertIn("CH1 12.6 V/div (now 26 V/div)", notes[1])
        self.assertFalse(HEX.search(" ".join(notes)))

    def test_status_bar(self):
        txt = labels.scope_status(state(), desc(), FakeFrame())
        self.assertEqual(txt, "Running · 2.56 ms/div · 12.49 kS/s (measured)")
        txt = labels.scope_status(None, desc(), FakeFrame(tb=0x08, rate=0, tier="none"))
        self.assertIn("timebase not calibrated", txt)


class TestNotes(unittest.TestCase):
    def test_note_words(self):
        self.assertEqual(labels.note_words("NO_CAPTURE_DATA"), "No capture yet")
        self.assertEqual(labels.note_words("timeout: no reply to 0x22 within 1.0 s"),
                         "No reply from the unit")
        self.assertNotIn("0x", labels.note_words("something odd 0x1F"))

    def test_job_words(self):
        self.assertEqual(labels.job_words("set", True, "timebase 16"), "Timebase changed")
        self.assertEqual(labels.job_words("set", False, "vdiv refused: BAD_ARG: x"),
                         "Volts/div not changed: the unit refused a value")
        self.assertEqual(labels.job_words("press", True, "MENU"), "Key press sent")


if __name__ == "__main__":
    unittest.main()
