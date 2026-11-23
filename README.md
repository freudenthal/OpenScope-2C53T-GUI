# OpenScope-2C53T-GUI — desktop window for the OpenScope 2C53T

`openscope-gui` is a Windows/Linux/macOS instrument window for a FNIRSI 2C53T that runs the
[OpenScope-2C53T](https://github.com/freudenthal/OpenScope-2C53T) firmware. It uses the USB CDC
remote protocol through the `openscope` host library. That library is in the firmware
repository, at `tools/openscope_host/`, so that the protocol code and the firmware change together.

![openscope-gui on the unit: Oscilloscope tab, control panel, status bar](docs/openscope-gui.png)

## Install
Put the two repositories next to each other:

```
<workspace>/
  OpenScope-2C53T/       firmware + tools/openscope_host (the openscope library)
  OpenScope-2C53T-GUI/   this repository
```

```bash
pip install -e ../OpenScope-2C53T/tools/openscope_host
pip install -e .
openscope-gui            # or: python -m openscope_gui.app
```

The `openscope` library is not on PyPI. Install it from your firmware checkout before this
package. The GUI needs a firmware build with the same protocol version as the library.

## The window
* **Tabs are the unit's modes:** Oscilloscope, Multimeter, Signal generator. Click a tab and the
  unit goes to that mode. Press MENU on the unit and the window goes to the matching tab (it
  sends nothing for that). While the unit shows its Settings menu, a line over the tabs says so.
* **Control panel** (right; one panel per tab). Oscilloscope: Run/Stop, Single (trigger Single,
  then run), the channels to show, time/div, volts/div and probe ×1/×10 per channel, trigger
  mode, edge, source and level. The lists show practical values from the unit's own description
  (`openscope describe`): "2.56 ms/div", "26 V/div" (probe included), "≈" for provisional values.
  Settings with no calibrated value are hidden; View ▸ Show uncalibrated settings lists them.
  The level is in volts on the source channel, relative to the middle of the screen (in percent
  of the screen on a range with no volts meaning). Codes (timebase code, range, register value)
  are in the tooltips only. The panel shows the state the unit reports back after each set, never
  the value it asked for. The Multimeter and Signal generator panels come in later stages.
* **Menus:** File (Save LCD screenshot, Save waveform as CSV or NPZ, Record meter to CSV, Exit),
  View (Control panel, Unit keys, Show uncalibrated settings, Advanced tools), Device (Connect,
  Disconnect, Port, Device information), Tools (with Advanced tools: Debug console, which never
  sends `cal restore` or `mem write`), Help (About). Window size, dock layout and View choices
  are kept between runs.
* **Toolbar and keys:** Run/Stop (Space), Single, Screenshot (Ctrl+P), Save waveform (Ctrl+S),
  Pause display; F1 About. Shortcuts never send unit key presses. The unit keys dock (View ▸
  Unit keys, hidden by default) sends key presses for functions that have no control yet.
* **Status bar:** connection, unit mode and battery, the scope state in words ("Running ·
  2.56 ms/div · 12.49 kS/s (measured)", "held record", "record taken at …" when the shown
  record was taken with other settings), link and draw rates.

The plot shows raw ADC counts, two cursors (Δt, Δcounts), an FFT tab and the measurements; the
meter tab has a strip chart and the CSV log. The screenshot stops the scope for the dump and runs
it again, so the screen passes the CRC. The window recovers by itself after an unplug. Bench
options: `--stats CSV` (rates once a second), `--events TXT`, `--meter-log CSV`, `--grab PNG`,
`--quit-after S`, `--channels 1,2`. One reader thread (`worker.py`) owns the port; a 30 Hz
timer draws.

## Layers
| module | job |
|---|---|
| `worker.py` | reader `QThread`: the only owner of the port; jobs in, frames and state out |
| `window.py` | tabs, menus, toolbar, docks |
| `scope_panel.py` | Oscilloscope control panel |
| `dialogs.py` | Device information, Debug console, About |
| `labels.py` | practical-unit text from the unit's description |
| `views.py` | plots, FFT, cursors, meter strip chart and CSV |
| `app.py` | entry point (`openscope-gui`) |

## Tests (no hardware)
```bash
python tests/test_gui_labels.py   # practical-unit text from a real device description (no Qt)
python tests/test_gui_worker.py   # reader thread vs a fake Device (skipped without PySide6/numpy)
python tests/test_gui_views.py    # FFT/period helpers, cursor readout, meter CSV (offscreen Qt)
python tests/test_gui_app.py      # the window offscreen: tabs = modes, panel, no codes visible, settings kept
python -m unittest discover -s tests -t tests   # all of them
```
If `openscope` is not installed, the suites find it in `../OpenScope-2C53T/tools/openscope_host`,
or in the folder that `OPENSCOPE_HOST` names.

## History
This code was in the firmware repository as `tools/openscope_host/openscope/gui` (package
`openscope.gui`) until it moved here. The first five commits replay that history.

## License
GPL-3.0, the same as OpenScope-2C53T. See `LICENSE`.
