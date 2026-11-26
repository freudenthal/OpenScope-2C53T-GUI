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
from typing import List, Optional, Sequence, Tuple

from openscope import proto

MODE_WORDS = {0: "Oscilloscope", 1: "Multimeter", 2: "Signal generator", 3: "Settings"}
TRIG_MODE_WORDS = {0: "Auto", 1: "Normal", 2: "Single"}
TRIG_EDGE_WORDS = {0: "Rising edge", 1: "Falling edge"}
TRIG_SOURCE_WORDS = {0: "CH1", 1: "CH2"}
# Stage-087 bench (run 3): guest-coldtrace before stage-091 fired on CH1
# whatever the source (G-320). Stage-091 (firmware 80565ab, reg 0x06): source
# CH2 fires on CH2 only (Normal, 4 s: level at the CH2 middle 14 / 4 records at
# 0x0F / 0x12, level at the CH1 middle 0 / 0). False marks CH2 for an older image.
CH2_TRIGGER_WORKS = True
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
    calibrated" if it has no rate), so the combo can show what is in force.
    Codes with the same text (0x00-0x04 all sample at 125 MS/s, stage-250) give
    one item: the lowest code, or `current` when it is one of them; the
    tooltip names the other codes (stage-255). `show_uncalibrated` lists every
    code, the duplicates too."""
    n = len(desc.rates) if desc and desc.rates else proto.TIMEBASE_CODES
    out: List[Item] = []
    same: dict = {}                     # text -> codes that give it
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
        item = Item(code, text, timebase_tooltip(desc, code), tier if t is not None else "none")
        if t is None:
            out.append(item)
            continue
        if text in same and not show_uncalibrated:
            if code == current:         # the code in force replaces the item for its rate
                out[next(j for j, it in enumerate(out) if it.text == text)] = item
        else:
            out.append(item)
        same.setdefault(text, []).append(code)
    return [_with_same(it, same.get(it.text, [])) for it in out]


def _with_same(it: Item, codes: List[int]) -> Item:
    others = [c for c in codes if c != it.value]
    if not others:
        return it
    names = ", ".join(f"0x{c:02X}" for c in others)
    return Item(it.value, it.text, f"{it.tooltip}; codes {names} give the same rate", it.tier)


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


def trigger_channel(state: Optional[proto.ScopeState]) -> int:
    """The channel (0 = CH1, 1 = CH2) whose record the unit compares with the
    level: the trigger source, or CH1 on a firmware where CH2 does not work."""
    if state is not None and state.trig_source == 1 and CH2_TRIGGER_WORKS:
        return 1
    return 0


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
    src = trigger_channel(state)
    shown = [w for w in frame.waves if w.channel == src]
    if fire is not None and shown:
        body = shown[0].samples[shown[0].head_skip:]
        if body and not (min(body) < fire <= max(body)):
            notes.append(f"trigger level outside the CH{src + 1} signal")
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
    ident = desc.identity if desc is not None else None
    if ident is not None:           # section 7 (stage-190): dialogs only, never the main window
        rows += [("Unit ID", ident.uid_hex),
                 ("Build target", ident.target),
                 ("Git revision", ident.git_rev),
                 ("API level", f"{ident.api_level} (this program needs {REQUIRED_API_LEVEL})")]
    else:
        rows.append(("Unit ID", "not reported by this firmware"))
    return rows


# ── firmware compatibility (stage-210) ─────────────────────────────────
# The API level (DESCRIPTION section 7, remote_protocol.md §9.2) that this GUI
# needs. Firmware with a lower level, or with no section 7, or with the shell
# only, needs an update.
REQUIRED_API_LEVEL = 1

UPDATE_NEEDED = "Firmware update needed"
UPDATE_RECOMMENDED = "Firmware update recommended"
COMPAT_TEXT = {
    "api_lower": f"{UPDATE_NEEDED}: the firmware on the unit is older than this program needs.",
    "no_identity": f"{UPDATE_NEEDED}: the firmware on the unit does not identify its build to "
                   "this program.",
    "shell_only": f"{UPDATE_NEEDED}: the firmware on the unit has no binary link for this program.",
    "iap": f"The unit waits in upgrade mode. {UPDATE_NEEDED}.",
    "stock": "The unit runs the FNIRSI firmware. Install OpenScope to use this program.",
    "api_higher": "This program is older than the firmware on the unit. Some functions can be "
                  "missing; get a newer program.",
    "recommended": f"{UPDATE_RECOMMENDED}: newer firmware comes with this program.",
}


@dataclass(frozen=True)
class Compat:
    """What the worker found about the firmware at connect, or about the
    drives while no port is found.

    kind: "binary" (connected), "shell_only" (an old OpenScope image: the
    shell answers, the binary protocol does not), "iap" (an IAP drive and no
    port: the unit waits in upgrade mode), "stock" (the stock firmware's USB
    Sharing drive and no port), "none" (nothing found)."""
    kind: str
    api_level: Optional[int] = None     # binary: section 7 API level; None = no section 7
    fw_version: str = ""                # binary: the STATUS firmware text


_BUILD = re.compile(r"OpenScope ([A-Z][a-z]{2}) +(\d{1,2}) (\d{4}) (\d\d:\d\d:\d\d)")


def build_time(text: Optional[str]):
    """The build time in a firmware text or a manifest build time
    ("Nov 25 2026 23:12:35"; __DATE__ pads the day with a space), as a
    datetime; None if there is none."""
    from datetime import datetime
    if not text:
        return None
    m = _BUILD.search(text if text.startswith("OpenScope") else "OpenScope " + text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {int(m.group(2))} {m.group(3)} {m.group(4)}",
                                 "%b %d %Y %H:%M:%S")
    except ValueError:
        return None


def bundled_is_newer(c: Compat, bundled) -> bool:
    """ASSUMPTION rule (stage-210): the bundled image is newer when its
    manifest has the same API level as the running firmware and a later build
    time than the running firmware text."""
    if bundled is None or c.kind != "binary" or c.api_level is None:
        return False
    if bundled.api_level != c.api_level:
        return False
    mine, theirs = build_time(c.fw_version), build_time(bundled.build_time)
    return mine is not None and theirs is not None and theirs > mine


def compat_case(c: Optional[Compat], bundled=None, required: int = REQUIRED_API_LEVEL) -> Optional[str]:
    """The banner case (a COMPAT_TEXT key), or None for no banner."""
    if c is None or c.kind == "none":
        return None
    if c.kind in ("shell_only", "iap", "stock"):
        return c.kind
    if c.api_level is None:
        return "no_identity"
    if c.api_level < required:
        return "api_lower"
    if c.api_level > required:
        return "api_higher"
    return "recommended" if bundled_is_newer(c, bundled) else None


def compat_text(c: Optional[Compat], bundled=None,
                required: int = REQUIRED_API_LEVEL) -> Optional[Tuple[str, bool]]:
    """(banner text, offer the update wizard) or None for no banner. Pure.
    Firmware newer than this GUI gets a note only (no wizard)."""
    case = compat_case(c, bundled, required)
    if case is None:
        return None
    return COMPAT_TEXT[case], case != "api_higher"


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
# stage-230: a scope set outside scope mode (NAK UNSUPPORTED_IN_MODE)
SCOPE_MODE_NEEDED = "scope settings need scope mode"
_HEX = re.compile(r"0x[0-9A-Fa-f]+")
_UID = re.compile(r"\b[0-9A-Fa-f]{24}\b")      # a unit ID (stage-210: dialogs only)


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
    return _UID.sub("…", _HEX.sub("…", text))


SET_WORDS = {"mode": "Unit mode", "timebase": "Timebase", "vdiv": "Volts/div", "trigger": "Trigger",
             "run": "Run/stop", "probe": "Probe"}


def job_words(job: str, ok: bool, msg: str) -> str:
    """A finished worker job in words."""
    if job == "set":
        what = msg.split(" ", 1)[0]
        name = SET_WORDS.get(what, "Setting")
        if ok:
            return f"{name} changed"
        if "UNSUPPORTED_IN_MODE" in msg:
            return f"{name} not changed: {SCOPE_MODE_NEEDED}"
        for nak, words in NOTE_WORDS.items():
            if nak in msg:
                return f"{name} not changed: {words.lower()}"
        return f"{name} not changed"
    if job == "screenshot":
        return f"Screenshot saved: {msg.split(' (')[0]}" if ok else f"Screenshot failed: {note_words(msg)}"
    if job == "press":
        return "Key press sent" if ok else f"Key press failed: {note_words(msg)}"
    if job == "backup":
        return "Calibration backup saved" if ok else f"Calibration backup failed: {note_words(msg)}"
    return f"{job} {'done' if ok else 'failed'}"


def mode_label_items(modes: Sequence[int] = (0, 1, 2)) -> List[str]:
    return [MODE_WORDS[m] for m in modes]


# ── backup dialog and update wizard texts (stage-210) ──────────────────
# The wizard pages are dialogs: they can name unit keys (MENU, Power).
WIZ = {
    "check_sub": "The unit, its firmware and the image that this wizard will install.",
    "no_unit": "No unit found. Connect the unit with USB. The page updates by itself.",
    "no_image": "No image. Choose an image (a .bin file with its .manifest.json next to it).",
    "backup_sub": "A verified backup of the calibration of this unit must exist before the copy.",
    "backup_running": "Backing up the calibration of this unit. Do not disconnect the unit.",
    "backup_disk": "The unit is in upgrade mode. Looking for an earlier backup of this unit on this "
                   "computer.",
    "backup_verified": "The backup is verified. Push Next and put the unit in upgrade mode.",
    "backup_ok": "The backup is saved and verified.",
    "backup_reminder": "Copy this folder to a second place (a USB stick or cloud storage). A lost "
                       "calibration cannot be made again.",
    "backup_not_connected": "The unit is not connected. Connect the unit and try again. For an old "
                            "firmware that this program cannot connect to, use Device > Update "
                            "firmware… (it backs up first).",
    "first_sub": "First install from the FNIRSI firmware. The backup comes after the first OpenScope "
                 "start, because the FNIRSI firmware cannot send the calibration.",
    "first_ack": "This unit is a FNIRSI 2C53T (see the label). I copied the files that I want to keep. "
                 "I will not press a key until this wizard says that the backup is done.",
    "enter_sub": "Put the unit in upgrade mode. The wizard finds the unit by itself.",
    "enter_openscope": "Keep USB connected. Hold MENU and tap Power. Keep MENU held until the "
                       "upgrade screen shows.\nIf it does not show: hold MENU, press the pinhole "
                       "reset with a pin, and keep MENU held for 3 to 5 s.\nDo not open the drive "
                       "of the unit in Explorer.",
    "enter_stock": "Keep USB connected. Hold MENU and tap Power. Keep MENU held until the upgrade "
                   "screen shows.\nDo not open the drive of the unit in Explorer.",
    "enter_c_sub": "The unit is in upgrade mode already.",
    "enter_c": "The unit waits in upgrade mode. The copy starts next.",
    "enter_waiting": "Waiting for the unit in upgrade mode…",
    "copy_sub": "The image goes to the unit in one write.",
    "copy_warn": "Do not unplug the unit. Do not open the drive of the unit.",
    "reboot_sub": "The unit programs the image and starts again. Then the wizard checks the firmware "
                  "and the calibration.",
    "ok": "The update is complete.",
    "problems": "The image was copied, but the check found differences.",
    "refused": "The update did not start. Nothing was written to the unit.",
    "cancelled": "Cancelled. Nothing was written to the unit.",
    "failed_before": "The update stopped before the copy. Nothing was written to the unit.",
    "failed_copied": "The image was copied, but the update did not finish. Read the text below.",
}
