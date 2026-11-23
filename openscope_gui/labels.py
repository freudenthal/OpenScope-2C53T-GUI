"""Text for the GUI in practical units (stage-087). Pure functions, no Qt.

Every value comes from the unit's own description (GET_DESCRIPTION, stage-086)
or from SCOPE_STATE / the waveform header: there is no host copy of the
timebase or range tables. Visible text uses words and units; register codes,
range numbers and hex go into the tooltips only, for diagnosis.

Rules:
  * a timebase code or range with no value (tier "none") is hidden from the
    lists unless the user asks for uncalibrated settings; then it shows as
    "Not calibrated (n)" with n the code or range number in decimal;
  * a provisional value is prefixed with "≈";
  * volts per division include the probe factor (the volts at the probe tip);
  * the trigger level is relative to the middle of the screen (the zero point
    is not calibrated); on a range with no volts meaning it is in percent of
    the screen height (the full 0..255 ADC span).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from openscope import proto

MODE_WORDS = {0: "Oscilloscope", 1: "Multimeter", 2: "Signal generator", 3: "Settings"}
TRIG_MODE_WORDS = {0: "Auto", 1: "Normal", 2: "Single"}
TRIG_EDGE_WORDS = {0: "Rising edge", 1: "Falling edge"}
TRIG_SOURCE_WORDS = {0: "CH1", 1: "CH2"}
# Stage-087 bench (run 3): guest-coldtrace fires on CH1 whatever the source
# setting (source CH2 with the level at the CH1 middle triggers as often as
# source CH1; level at the CH2 middle: no records). Set True when a firmware
# makes the CH2 source work (G-320).
CH2_TRIGGER_WORKS = False
CH2_SOURCE_NOTE = "the unit triggers on CH1 only (CH2 source does not work yet)"
TIER_WORDS = {"measured": "measured", "provisional": "provisional", "none": "not calibrated"}
SCREEN_COUNTS = 256             # ADC span of the screen height (8 bit record)
APPROX = "≈ "
SEP = " · "


@dataclass(frozen=True)
class Item:
    """One combo box entry: the value sent to the unit, the visible text and
    the tooltip (which carries the code)."""
    value: int
    text: str
    tooltip: str
    tier: str


# ── numbers ───────────────────────────────────────────────────────────
def _sig(x: float, digits: int = 3) -> str:
    return f"{x:.{digits}g}"


def fmt_time(s: Optional[float]) -> str:
    """0.00256 -> '2.56 ms'; None -> 'not calibrated'."""
    if s is None:
        return "not calibrated"
    for unit, k in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6), ("ns", 1e-9)):
        if abs(s) >= k:
            return f"{_sig(s / k)} {unit}"
    return f"{_sig(s / 1e-9)} ns"


def fmt_time_div(s: Optional[float]) -> str:
    return "timebase not calibrated" if s is None else f"{fmt_time(s)}/div"


def fmt_rate(hz: Optional[float]) -> str:
    """12490 -> '12.49 kS/s'; 500 -> '500 S/s'."""
    if not hz:
        return "no trusted sample rate"
    if hz >= 1e6:
        return f"{hz / 1e6:.4g} MS/s"
    if hz >= 1e3:
        return f"{hz / 1e3:.4g} kS/s"
    return f"{hz:.4g} S/s"


def fmt_volts(v: Optional[float], signed: bool = False) -> str:
    """1.37 -> '1.37 V'; 0.0429 -> '42.9 mV'."""
    if v is None:
        return "no volts meaning"
    sign = "+" if signed and v > 0 else ""
    if v == 0:
        return "0 V"
    if abs(v) >= 1.0:
        return f"{sign}{_sig(v)} V"
    return f"{sign}{_sig(v * 1e3)} mV"


def fmt_volts_div(v: Optional[float]) -> str:
    return "no volts meaning" if v is None else f"{fmt_volts(v)}/div"


def tier_word(tier: str) -> str:
    return TIER_WORDS.get(tier, tier)


def _approx(tier: str) -> str:
    return APPROX if tier == "provisional" else ""


# ── combo items ───────────────────────────────────────────────────────
def timebase_tooltip(desc: Optional[proto.Description], code: int) -> str:
    rate = desc.sample_rate(code) if desc else None
    tier = desc.tier("timebase", code) if desc else "none"
    r = f"{rate:,} samples/s" if rate else "no sample rate"
    return f"Timebase code 0x{code:02X}, {r}, {tier_word(tier)}"


def timebase_items(desc: Optional[proto.Description], show_uncalibrated: bool = False,
                   current: Optional[int] = None) -> List[Item]:
    """Timebase list, fastest first. `current` is always listed (as "Not
    calibrated" if it has no rate), so the combo can show what is in force."""
    n = len(desc.rates) if desc and desc.rates else proto.TIMEBASE_CODES
    out = []
    for code in range(n):
        t = desc.time_per_div(code) if desc else None
        tier = desc.tier("timebase", code) if desc else "none"
        if t is not None:
            text = _approx(tier) + fmt_time_div(t)
        elif show_uncalibrated:
            text = f"Not calibrated ({code})"
        elif code == current:
            text = "Not calibrated"
        else:
            continue
        out.append(Item(code, text, timebase_tooltip(desc, code), tier if t is not None else "none"))
    return out


def vdiv_tooltip(desc: Optional[proto.Description], channel: int, rng: int, probe: int) -> str:
    uv = desc.uv_per_div_bnc(channel, rng) if desc else None
    tier = desc.tier("range", channel, rng) if desc else "none"
    if uv and desc.counts_per_div:
        g = f"{uv / 1000 / desc.counts_per_div:.4g} mV/count at the BNC"
    else:
        g = "no volts meaning"
    return f"Range {rng}, {g}, {tier_word(tier)}, probe ×{probe}"


def vdiv_items(desc: Optional[proto.Description], channel: int, probe: int,
               show_uncalibrated: bool = False, current: Optional[int] = None) -> List[Item]:
    """Volts/div list of one channel, most sensitive first, probe included."""
    n = len(desc.ranges[channel - 1]) if desc and len(desc.ranges) >= channel else proto.VDIV_RANGES
    rows = []
    for rng in range(n):
        v = desc.volts_per_div(channel, rng, probe) if desc else None
        tier = desc.tier("range", channel, rng) if desc else "none"
        if v is not None:
            text = _approx(tier) + fmt_volts_div(v)
        elif show_uncalibrated:
            text = f"Not calibrated ({rng})"
        elif rng == current:
            text = "Not calibrated"
        else:
            continue
        rows.append((v if v is not None else float("inf"), rng,
                     Item(rng, text, vdiv_tooltip(desc, channel, rng, probe),
                          tier if v is not None else "none")))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows]


def probe_items() -> List[Item]:
    return [Item(f, f"×{f}", f"Probe factor {f} (the unit's own probe setting)", "measured")
            for f in proto.PROBE_FACTORS]


def word_items(words: dict, what: str) -> List[Item]:
    return [Item(k, v, f"{what} {k}", "measured") for k, v in sorted(words.items())]


def source_items() -> List[Item]:
    items = word_items(TRIG_SOURCE_WORDS, "Trigger source")
    if not CH2_TRIGGER_WORKS:
        items[1] = Item(1, "CH2 (not working yet)",
                        "Trigger source 1. This firmware fires on CH1 whatever the source: the "
                        "level is compared with CH1 (G-320)", "none")
    return items


def level_for_counts(desc: Optional[proto.Description], counts: float) -> Optional[int]:
    """The UI level whose firing point (code + record offset) is nearest `counts`."""
    if not desc or not desc.trigger:
        return None
    t = desc.trigger
    return min(range(t.level_min, t.level_max + 1), key=lambda lv: abs(t.record_counts(lv) - counts))


def trigger_counts(desc: Optional[proto.Description], state: Optional[proto.ScopeState]) -> Optional[int]:
    """Record count where the unit fires at the level in force (the plot's y)."""
    if not desc or not desc.trigger or state is None:
        return None
    return desc.trigger.record_counts(state.trig_level)


# ── trigger level ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class LevelScale:
    """How the level spin box shows the unit's level (-100..100): value =
    level * step, in `unit` ("V" or "%"), relative to the screen middle."""
    step: float
    unit: str
    decimals: int
    level_min: int
    level_max: int

    def value(self, level: int) -> float:
        return level * self.step

    def level(self, value: float) -> int:
        lv = int(round(value / self.step)) if self.step else 0
        return max(self.level_min, min(self.level_max, lv))

    def text(self, level: int) -> str:
        v = self.value(level)
        if self.unit == "%":
            return f"{v:+.{self.decimals}f} % of screen"
        return fmt_volts(v, signed=True)


def _decimals(step: float) -> int:
    import math
    if step <= 0:
        return 0
    return max(0, min(4, 1 - int(math.floor(math.log10(step)))))


def level_scale(desc: Optional[proto.Description], state: Optional[proto.ScopeState]) -> LevelScale:
    """Volts per level step on the trigger source channel, with the live probe."""
    tmin, tmax = -proto.TRIG_LEVEL_MAX, proto.TRIG_LEVEL_MAX
    cpl = 256 / 206
    if desc and desc.trigger:
        tmin, tmax, cpl = desc.trigger.level_min, desc.trigger.level_max, desc.trigger.counts_per_level
    vpl = None
    if desc and state is not None:
        ch = state.trig_source + 1
        probe = state.probe[state.trig_source] if state.probe else None
        vpl = desc.volts_per_level(ch, state.ch[state.trig_source].vdiv_idx, probe)
    if vpl:
        return LevelScale(vpl, "V", _decimals(vpl), tmin, tmax)
    pct = cpl / SCREEN_COUNTS * 100
    return LevelScale(pct, "%", _decimals(pct), tmin, tmax)


def level_tooltip(state: Optional[proto.ScopeState]) -> str:
    if state is None:
        return "Trigger level, relative to the middle of the screen"
    return (f"Trigger level {state.trig_level:+d} (register 0x08 = 0x{state.trig_code:02X}), relative "
            "to the middle of the screen: the zero point is not calibrated")


# ── state and status text ─────────────────────────────────────────────
def channel_words(state: proto.ScopeState, i: int) -> str:
    c = state.ch[i]
    probe = f"probe ×{state.probe[i]}" if state.probe else "probe not reported"
    if c.uv_per_div:           # SCOPE_STATE uv_per_div includes the probe (C-100)
        return f"CH{i + 1} {_approx(c.tier_name)}{fmt_volts_div(c.uv_per_div / 1e6)} ({probe}, {tier_word(c.tier_name)})"
    return f"CH{i + 1} no volts meaning on this range ({probe})"


def run_words(state: proto.ScopeState) -> str:
    if state.trig_mode == 2:        # Single: "running" on the unit means armed or holding
        return "Single capture (Run returns to continuous)"
    return "Running" if state.running else "Stopped (held record)"


def timebase_words(state: proto.ScopeState, desc: Optional[proto.Description]) -> List[str]:
    t = desc.time_per_div(state.timebase_code) if desc else None
    parts = [_approx(state.timebase_tier_name) + fmt_time_div(t) if t is not None
             else "timebase not calibrated"]
    if state.sample_rate_hz:
        parts.append(f"{fmt_rate(state.sample_rate_hz)} ({tier_word(state.timebase_tier_name)})")
    else:
        parts.append("no trusted sample rate")
    if state.timebase_disagrees:
        parts.append("the unit's screen shows another timebase")
    return parts


def trigger_words(state: proto.ScopeState, desc: Optional[proto.Description]) -> str:
    ls = level_scale(desc, state)
    return SEP.join([f"Trigger {TRIG_MODE_WORDS.get(state.trig_mode, '?')}",
                     TRIG_EDGE_WORDS.get(state.trig_edge, "?"),
                     TRIG_SOURCE_WORDS.get(state.trig_source, "?")
                     + (" (not working yet)" if state.trig_source == 1 and not CH2_TRIGGER_WORKS else ""),
                     f"level {ls.text(state.trig_level)}"])


def state_lines(state: proto.ScopeState, desc: Optional[proto.Description]) -> List[str]:
    """The oscilloscope panel's state text, in words."""
    return [SEP.join([run_words(state)] + timebase_words(state, desc)),
            channel_words(state, 0),
            channel_words(state, 1),
            trigger_words(state, desc)]


def frame_notes(frame, state: Optional[proto.ScopeState], desc: Optional[proto.Description]) -> List[str]:
    """Trust notes for a shown frame against the state in force, in words.
    `frame` is a gui.worker.ScopeFrame (duck-typed: timebase_idx, waves,
    sample_rate_hz, timebase_tier)."""
    notes = []
    if not frame.sample_rate_hz:
        notes.append("timebase not calibrated")
    elif frame.timebase_tier == "provisional":
        notes.append("sample rate provisional")
    if frame.waves and frame.waves[0].timebase_disagrees:
        notes.append("rate withheld: the unit's screen shows another timebase")
    if state is None:
        return notes
    fire = trigger_counts(desc, state)
    ch1 = [w for w in frame.waves if w.channel == 0]
    if fire is not None and ch1:
        body = ch1[0].samples[ch1[0].head_skip:]
        if body and not (min(body) < fire <= max(body)):
            notes.append("trigger level outside the CH1 signal")
    if state.trig_source == 1 and not CH2_TRIGGER_WORKS:
        notes.append(CH2_SOURCE_NOTE)
    if state.trig_mode == 2:
        notes.insert(0, "single record")
    elif not state.running:
        notes.insert(0, "held record")
    diffs = []
    if frame.timebase_idx != state.timebase_code:
        then = desc.time_per_div(frame.timebase_idx) if desc else None
        now = desc.time_per_div(state.timebase_code) if desc else None
        diffs.append(f"{fmt_time_div(then)} (now {fmt_time_div(now)})" if then and now
                     else "another timebase")
    for w in frame.waves:
        now = state.ch[w.channel]
        if w.vdiv_idx != now.vdiv_idx:
            then_v = w.uv_per_div / 1e6 if w.uv_per_div else None
            now_v = now.uv_per_div / 1e6 if now.uv_per_div else None
            diffs.append(f"{w.channel_name} {fmt_volts_div(then_v)} (now {fmt_volts_div(now_v)})"
                         if then_v and now_v else f"{w.channel_name} another range")
    if diffs:
        notes.append("record taken at " + ", ".join(diffs))
    return notes


def scope_status(state: Optional[proto.ScopeState], desc: Optional[proto.Description], frame=None) -> str:
    """Status bar text, e.g. 'Running · 2.56 ms/div · 12.49 kS/s (measured)'."""
    if state is not None:
        head = [run_words(state).split(" (")[0]] + timebase_words(state, desc)
    elif frame is not None:
        t = desc.time_per_div(frame.timebase_idx) if desc else None
        head = [fmt_time_div(t), f"{fmt_rate(frame.sample_rate_hz)} ({tier_word(frame.timebase_tier)})"
                if frame.sample_rate_hz else "no trusted sample rate"]
    else:
        return ""
    notes = frame_notes(frame, state, desc) if frame is not None else []
    return SEP.join(head + notes)


def battery_words(st: proto.Status) -> str:
    if st.battery_known:
        b = f"Battery {st.battery_pct} %"
        if st.battery_critical:
            b += " (critical)"
    else:
        b = "Battery unknown"
    return b + (", charging" if st.charging else "")


def info_lines(version: str, port: str, st: proto.Status, state: Optional[proto.ScopeState],
               desc: Optional[proto.Description]) -> List[tuple]:
    """Device information dialog rows (label, value): the `openscope info` data."""
    probe = state.probe if state is not None and state.probe else (desc.probe if desc else None)
    rows = [("Port", port or "unknown"),
            ("Firmware", version),
            ("Protocol", f"version {st.proto_version}"),
            ("Unit mode", MODE_WORDS.get(st.mode, "unknown")),
            ("Battery", battery_words(st) + (f" ({st.battery_mv} mV)" if st.battery_known else "")),
            ("Capture", "real samples" if st.capture_ready else "no capture data yet"),
            ("Running for", fmt_duration(st.uptime_ms / 1000)),
            ("USB health", f"{st.usb_tx_stalls} transmit stalls, {st.usb_heals} self-heals"),
            ("Probe settings", ", ".join(f"CH{i + 1} ×{f}" for i, f in enumerate(probe))
             if probe else "not reported by this firmware"),
            ("Description", "received" if desc else "not sent by this firmware")]
    return rows


def fmt_duration(s: float) -> str:
    s = int(s)
    h, m = divmod(s // 60, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s % 60} s"
    return f"{s} s"


NOTE_WORDS = {
    "NO_CAPTURE_DATA": "No capture yet",
    "NOT_READY": "The unit is busy; trying again",
    "UNSUPPORTED_IN_MODE": "Not available in this unit mode",
    "BAD_ARG": "The unit refused a value",
    "UNSUPPORTED": "This firmware does not do that",
}
_HEX = re.compile(r"0x[0-9A-Fa-f]+")


def note_words(text: str) -> str:
    """A worker note or error in words for the status bar (the raw text goes
    to the tooltip): NAK names become sentences, hex codes are removed."""
    for name, words in NOTE_WORDS.items():
        if text.startswith(name):
            return words
    low = text.lower()
    if low.startswith("timeout") or "timeouts in a row" in low:
        return "No reply from the unit"
    if low.startswith("protocol"):
        return "Bad reply from the unit; trying again"
    return _HEX.sub("…", text)


SET_WORDS = {"mode": "Unit mode", "timebase": "Timebase", "vdiv": "Volts/div", "trigger": "Trigger",
             "run": "Run/stop", "probe": "Probe"}


def job_words(job: str, ok: bool, msg: str) -> str:
    """A finished worker job in words."""
    if job == "set":
        what = msg.split(" ", 1)[0]
        name = SET_WORDS.get(what, "Setting")
        if ok:
            return f"{name} changed"
        for nak, words in NOTE_WORDS.items():
            if nak in msg:
                return f"{name} not changed: {words.lower()}"
        return f"{name} not changed"
    if job == "screenshot":
        return f"Screenshot saved: {msg.split(' (')[0]}" if ok else f"Screenshot failed: {note_words(msg)}"
    if job == "press":
        return "Key press sent" if ok else f"Key press failed: {note_words(msg)}"
    return f"{job} {'done' if ok else 'failed'}"


def mode_label_items(modes: Sequence[int] = (0, 1, 2)) -> List[str]:
    return [MODE_WORDS[m] for m in modes]
