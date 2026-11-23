"""`openscope-gui`: control and live view of the unit over USB CDC.

Stage-087: a normal Windows instrument window (window.py). The tabs are the
unit's modes; each tab has its control panel; menus hold the other functions
(screenshot, save waveform, meter log, port, device information, debug
console); values show in practical units from the unit's own description.
One reader thread owns the port (worker.py); a 30 Hz timer draws the latest
data.
"""
from __future__ import annotations

import argparse
import sys

from PySide6 import QtCore, QtWidgets

from .. import proto
from .window import KEYS, MainWindow  # noqa: F401  (re-exported for scripts)
from .worker import Worker


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openscope-gui", description=__doc__.splitlines()[0])
    p.add_argument("--port", help="serial port (default: find the unit by USB VID:PID)")
    p.add_argument("--channels", default="1", help="channels to show: 1, 2 or 1,2 (default 1)")
    p.add_argument("--stats", metavar="CSV", help="append link/draw rates once a second (bench)")
    p.add_argument("--events", metavar="TXT", help="append connection changes, notes and jobs (bench)")
    p.add_argument("--meter-log", metavar="CSV", help="start the meter CSV log at launch")
    p.add_argument("--grab", metavar="PNG", help="save a window image after --grab-after s")
    p.add_argument("--grab-after", type=float, default=10.0)
    p.add_argument("--quit-after", type=float, metavar="S", help="close after S seconds (bench)")
    a = p.parse_args(argv)
    from ..device import Device
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=False, foreground="#dddddd", background="#1e1e1e")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setOrganizationName("OpenScope")
    app.setApplicationName("openscope-gui")
    worker = Worker(Device.open)
    worker.port = a.port
    mask = proto.channel_mask(a.channels)
    win = MainWindow(worker, a.stats, a.events)
    if a.meter_log:
        win.meter.start_log(a.meter_log)
        win.meter.log_btn.blockSignals(True)
        win.meter.log_btn.setChecked(True)
        win.meter.log_btn.setText("Stop log")
        win.meter.log_btn.blockSignals(False)
        win.act_meter_log.blockSignals(True)
        win.act_meter_log.setChecked(True)
        win.act_meter_log.blockSignals(False)
    win.panel.set_channels(mask)
    worker.set_channels(mask)
    win.show()
    worker.start()
    if a.grab:
        QtCore.QTimer.singleShot(int(a.grab_after * 1000), lambda: win._grab(a.grab))
    if a.quit_after:
        QtCore.QTimer.singleShot(int(a.quit_after * 1000), win.close)
    rc = app.exec()
    worker.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
