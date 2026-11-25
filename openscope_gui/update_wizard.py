"""Calibration backup and the firmware update wizard (stage-210).

BackupDialog runs the library's calbackup.make_backup() as a worker job (the
worker owns the port) and shows the folder, the CRC32 values and the unit ID.

UpdateWizard guides one update with the library flow (update.run_update for
paths A and C, update.run_first_install for path F). An UpdateRunner thread
runs the flow with its own Device; before it starts, the main worker goes
offline (port closed) and stays offline until the wizard closes, so the
worker sends nothing to the unit during the update. The runner stops the flow
at the first "enter" (or "copy") step until the user presses Next on the
backup page: no image is copied before a verified backup of this run exists
(paths A, C) or before the first-install acknowledgment (path F).

Pages: 1 Check, 2 Backup, 3 Upgrade mode, 4 Copy, 5 Restart, 6 Result. From the
copy on, the wizard has no Cancel and cannot close until the flow ends (an
extra write to the drive during the copy damages the image). The GUI never
opens or lists files on the IAP drive: the library does the drive work.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from openscope import firmware as fw
from openscope import update as up
from openscope.device import Cancelled

from . import labels

PICTURES = Path(__file__).with_name("pictures")
CHECK, BACKUP, ENTER, COPY, REBOOT, RESULT = range(6)
PAGE_NAMES = ("check", "backup", "upgrade mode", "copy", "restart", "result")
FIRMWARE_DIR_ENV = "OPENSCOPE_FIRMWARE_DIR"
COPY_SECONDS = 14                   # a copy takes 12-14 s (stage-030), 10.7 s on unit #1 (stage-200)


# ── images ─────────────────────────────────────────────────────────────
def bundled_firmware_dir() -> Optional[Path]:
    """Frozen (release package): <exe dir>/firmware. Else the folder in
    OPENSCOPE_FIRMWARE_DIR. Else None."""
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).resolve().parent / "firmware"
        return d if d.is_dir() else None
    env = os.environ.get(FIRMWARE_DIR_ENV)
    if env and Path(env).is_dir():
        return Path(env)
    return None


def find_images(folder: Optional[Path]) -> List[Tuple[Path, fw.Manifest]]:
    """(image, manifest) for each manifest in `folder` whose image exists,
    newest build first."""
    out = []
    if folder is None or not Path(folder).is_dir():
        return out
    for m in sorted(Path(folder).glob("*" + fw.MANIFEST_SUFFIX)):
        try:
            man = fw.Manifest.load(m)
            img = fw.image_path_for(m, man)
        except fw.ManifestError:
            continue
        if img.is_file():
            out.append((img, man))
    def key(im):
        t = labels.build_time(im[1].build_time)
        return (t is not None, t.timestamp() if t else 0.0)
    out.sort(key=key, reverse=True)
    return out


def newest_bundled() -> Optional[fw.Manifest]:
    imgs = find_images(bundled_firmware_dir())
    return imgs[0][1] if imgs else None


class Image:
    """One target image: bytes, manifest, the file name on the drive and the
    refusals of firmware.check_image() (empty = it can go to the unit)."""

    def __init__(self, path: Path, check: Callable = fw.check_image):
        self.path = Path(path)
        self.manifest: Optional[fw.Manifest] = None
        self.data = b""
        self.refusals: List[str] = []
        try:
            if self.path.name.endswith(fw.MANIFEST_SUFFIX):
                self.manifest = fw.Manifest.load(self.path)
                self.path = fw.image_path_for(self.path, self.manifest)
            else:
                mp = fw.manifest_path_for(self.path)
                if mp.is_file():
                    self.manifest = fw.Manifest.load(mp)
            self.data = self.path.read_bytes()
        except (OSError, fw.ManifestError) as e:
            self.refusals.append(str(e))
            return
        self.name = self.path.name
        self.refusals += check(self.data, self.manifest)
        try:
            up.iapdrive.check_name(self.name)
        except up.iapdrive.DriveError as e:
            self.refusals.append(str(e))

    @property
    def ok(self) -> bool:
        return not self.refusals

    def words(self) -> str:
        m = self.manifest
        if m is None:
            return f"{self.path.name} (no manifest)"
        return f"{m.version}, built {m.build_time or 'unknown'}, API level {m.api_level}"


def path_for(c: Optional[labels.Compat]) -> Optional[str]:
    """The library path for the start state: A (a running OpenScope image),
    C (upgrade mode, no port), F (stock firmware: first install)."""
    if c is None:
        return None
    return {"binary": "A", "shell_only": "A", "iap": "C", "stock": "F"}.get(c.kind)


# ── calibration backup (Device > Back up calibration…) ─────────────────
class BackupDialog(QtWidgets.QDialog):
    """Runs the worker's backup job and shows the result."""

    def __init__(self, worker, connected: bool, parent=None, root=None):
        super().__init__(parent)
        self.worker = worker
        self.folder: Optional[str] = None
        self.running = False
        self.setWindowTitle("Back up calibration")
        lay = QtWidgets.QVBoxLayout(self)
        self.text = QtWidgets.QLabel(labels.WIZ["backup_running"])
        self.text.setWordWrap(True)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        self.form = QtWidgets.QFormLayout()
        self.reminder = QtWidgets.QLabel(labels.WIZ["backup_reminder"])
        self.reminder.setWordWrap(True)
        self.reminder.setStyleSheet("font-weight:bold")
        self.reminder.hide()
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.open_btn = bb.addButton("Open folder", QtWidgets.QDialogButtonBox.ActionRole)
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_folder)
        bb.rejected.connect(self.reject)
        for w in (self.text, self.bar):
            lay.addWidget(w)
        lay.addLayout(self.form)
        lay.addWidget(self.reminder)
        lay.addWidget(bb)
        self.resize(560, 260)
        worker.backup_progress.connect(self.progress)
        worker.backup_result.connect(self.result)
        if connected:
            self.running = True
            worker.backup(root)
        else:
            self.bar.hide()
            self.text.setText(labels.WIZ["backup_not_connected"])

    def progress(self, part: str, done: int, total: int) -> None:
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)

    def result(self, r: dict) -> None:
        self.running = False
        self.bar.hide()
        if "error" in r:
            self.text.setText(f"The backup failed: {labels.note_words(r['error'])}")
            return
        self.folder = r["folder"]
        self.text.setText(labels.WIZ["backup_ok"] if r["verified"] else
                          "The backup does not verify: " + "; ".join(r["problems"]))
        for label, value in (("Folder", r["folder"]), ("Unit ID", r["uid"]),
                             ("Cal page CRC32", r["cal_crc32"]),
                             ("Cal block CRC32", r["block_crc32"])):
            v = QtWidgets.QLabel(str(value))
            v.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.form.addRow(label, v)
        self.open_btn.setEnabled(True)
        self.reminder.show()

    def open_folder(self) -> None:
        if self.folder:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.folder))

    def done(self, r: int) -> None:
        if self.running:
            self.worker.cancel_backup()
        for sig, slot in ((self.worker.backup_progress, self.progress),
                          (self.worker.backup_result, self.result)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(r)


# ── the runner thread ──────────────────────────────────────────────────
class UpdateRunner(QtCore.QThread):
    """Runs one library update flow with the library callbacks. The flow
    stops at the first step in `gate_steps` until release(); cancel() stops
    it before the copy (the library checks cancel only before the copy)."""
    event = QtCore.Signal(object)       # update.Event
    gate = QtCore.Signal(str)           # the flow waits for the user at this step
    result = QtCore.Signal(object)      # update.UpdateResult
    error = QtCore.Signal(object)       # the exception that stopped the flow

    def __init__(self, fn: Callable, args: tuple, kwargs: dict, gate_steps=(), parent=None):
        super().__init__(parent)
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.gate_steps = tuple(gate_steps)
        self._go = threading.Event()
        self._cancel = threading.Event()
        self._gated = False

    def run(self) -> None:
        try:
            res = self.fn(*self.args, report=self._report, cancel=self._cancel.is_set, **self.kwargs)
        except BaseException as e:      # Refused, Cancelled, UpdateError, DeviceError, NoDevice...
            self.error.emit(e)
            return
        self.result.emit(res)

    def _report(self, e) -> None:
        if e.kind == "step" and e.step in self.gate_steps and not self._gated:
            self._gated = True
            self.gate.emit(e.step)
            self._go.wait()
            if self._cancel.is_set():
                raise Cancelled("cancelled by the user before the copy; nothing was written")
        self.event.emit(e)

    def release(self) -> None:
        self._go.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._go.set()


# ── pages ──────────────────────────────────────────────────────────────
def _label(text: str = "", bold: bool = False) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    if bold:
        lbl.setStyleSheet("font-weight:bold")
    return lbl


class _Page(QtWidgets.QWizardPage):
    def wiz(self) -> "UpdateWizard":
        return self.wizard()

    def nextId(self) -> int:
        w = self.wiz()
        if w is None:
            return -1
        if w.failed and self.pageId() < RESULT:
            return RESULT
        return self.pageId() + 1 if self.pageId() < RESULT else -1

    def pageId(self) -> int:
        return self.property("page_id")

    def validatePage(self) -> bool:
        """QWizard.next() does not look at isComplete(): refuse here too."""
        return self.isComplete()


class CheckPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Check")
        self.setSubTitle(labels.WIZ["check_sub"])
        self.form = QtWidgets.QFormLayout()
        self.unit = _label()
        self.firmware = _label()
        self.compat = _label()
        self.combo = QtWidgets.QComboBox()
        self.choose = QtWidgets.QPushButton("Choose image…")
        self.image = _label()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.combo, 1)
        row.addWidget(self.choose)
        self.form.addRow("Unit", self.unit)
        self.form.addRow("Firmware now", self.firmware)
        self.form.addRow("Compatibility", self.compat)
        self.form.addRow("Target image", row)
        self.form.addRow("", self.image)
        self.setLayout(self.form)
        self.setCommitPage(True)        # no Back to this page once the flow starts
        self.combo.currentIndexChanged.connect(self._combo)
        self.choose.clicked.connect(self._choose)

    def initializePage(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        w = self.wiz()
        c, d = w.compat, w.desc
        ident = d.identity if d is not None else None
        kind = c.kind if c is not None else "none"
        self.unit.setText({
            "binary": (f"FNIRSI 2C53T, unit ID {ident.uid_hex}" if ident else
                       "FNIRSI 2C53T (this firmware does not give the unit ID)"),
            "shell_only": "FNIRSI 2C53T with old OpenScope firmware",
            "iap": "FNIRSI 2C53T in upgrade mode (no firmware runs)",
            "stock": "FNIRSI unit with the FNIRSI firmware",
        }.get(kind, labels.WIZ["no_unit"]))
        if kind == "binary":
            fwt = w.fw_version or (c.fw_version if c else "") or "unknown"
            if ident:
                fwt += f"\nbuild {ident.target} at {ident.git_rev}, API level {ident.api_level}"
            self.firmware.setText(fwt)
        else:
            self.firmware.setText({"shell_only": "old OpenScope image (debug shell only)",
                                   "iap": "upgrade mode", "stock": "FNIRSI stock firmware"
                                   }.get(kind, "unknown"))
        ct = labels.compat_text(c, w.image.manifest if w.image else None)
        self.compat.setText(ct[0] if ct else
                            ("This firmware works with this program." if kind == "binary" else
                             labels.WIZ["no_unit"]))
        self._fill_images()
        self.completeChanged.emit()

    def _fill_images(self) -> None:
        w = self.wiz()
        self.combo.blockSignals(True)
        self.combo.clear()
        for path, man in w.images:
            self.combo.addItem(f"{man.version} ({path.name})", str(path))
        if w.image is not None and self.combo.findData(str(w.image.path)) < 0:
            self.combo.addItem(w.image.path.name, str(w.image.path))
        if w.image is not None:
            self.combo.setCurrentIndex(self.combo.findData(str(w.image.path)))
        self.combo.blockSignals(False)
        if w.image is None and self.combo.count():
            self._combo(0)
        self._show_image()

    def _combo(self, i: int) -> None:
        if i >= 0 and self.combo.itemData(i):
            self.wiz().set_image(self.combo.itemData(i))
            self._show_image()
            self.completeChanged.emit()

    def _choose(self) -> None:
        path = self.wiz().choose_file()
        if path:
            self.wiz().set_image(path)
            self._fill_images()
            self.completeChanged.emit()

    def _show_image(self) -> None:
        img = self.wiz().image
        if img is None:
            self.image.setText(labels.WIZ["no_image"])
        elif img.ok:
            self.image.setText(f"{img.words()}\nThe image checks pass.")
        else:
            self.image.setText(f"{img.words()}\nThis image cannot go to the unit: "
                               + "; ".join(img.refusals))

    def isComplete(self) -> bool:
        w = self.wiz()
        return w is not None and w.path is not None and w.image is not None and w.image.ok


class BackupPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Backup")
        lay = QtWidgets.QVBoxLayout(self)
        self.text = _label()
        self.bar = QtWidgets.QProgressBar()
        self.result = _label(bold=True)
        self.ack = QtWidgets.QCheckBox(labels.WIZ["first_ack"])
        self.ack.setStyleSheet("QCheckBox { font-weight: bold }")
        self.typed = QtWidgets.QLineEdit()
        self.typed.setPlaceholderText(f"type {up.FIRST_INSTALL_ACK}")
        for wd in (self.text, self.bar, self.result, self.ack, self.typed):
            lay.addWidget(wd)
        lay.addStretch(1)
        self.setCommitPage(True)
        self.ack.toggled.connect(lambda _: self.completeChanged.emit())
        self.typed.textChanged.connect(lambda _: self.completeChanged.emit())

    def initializePage(self) -> None:
        w = self.wiz()
        first = w.path == "F"
        self.ack.setVisible(first)
        self.typed.setVisible(first)
        self.bar.setVisible(not first)
        if first:
            self.setSubTitle(labels.WIZ["first_sub"])
            self.text.setText(up.STOCK_FILES_TEXT + "\n\n" + up.NO_KEYS_TEXT)
        else:
            self.setSubTitle(labels.WIZ["backup_sub"])
            self.text.setText(labels.WIZ["backup_running"] if w.path == "A" else labels.WIZ["backup_disk"])
            self.bar.setRange(0, 0)
            w.start_runner()

    def show_progress(self, done: Optional[int], total: Optional[int]) -> None:
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done or 0)

    def isComplete(self) -> bool:
        w = self.wiz()
        if w is None:
            return False
        if w.failed:
            return True
        if w.path == "F":
            return self.ack.isChecked() and self.typed.text().strip() == up.FIRST_INSTALL_ACK
        return w.gate_step is not None

    def validatePage(self) -> bool:
        w = self.wiz()
        if not self.isComplete():
            return False
        if w.failed:
            return True
        if w.path == "F":
            w.start_runner()
        elif w.runner is not None:
            w.runner.release()
        return True


class EnterPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Upgrade mode")
        lay = QtWidgets.QVBoxLayout(self)
        self.text = _label()
        self.pics = QtWidgets.QHBoxLayout()
        self.status = _label(bold=True)
        lay.addWidget(self.text)
        lay.addLayout(self.pics)
        lay.addWidget(self.status)
        lay.addStretch(1)
        self.pic_labels: List[QtWidgets.QLabel] = []

    def initializePage(self) -> None:
        w = self.wiz()
        for lbl in self.pic_labels:
            lbl.deleteLater()
        self.pic_labels = []
        if w.path == "C":
            self.setSubTitle(labels.WIZ["enter_c_sub"])
            self.text.setText(labels.WIZ["enter_c"])
            names = []
        elif w.path == "F":
            self.setSubTitle(labels.WIZ["enter_sub"])
            self.text.setText(labels.WIZ["enter_stock"])
            names = ["upgrade_menu_power.svg"]
        else:
            self.setSubTitle(labels.WIZ["enter_sub"])
            self.text.setText(labels.WIZ["enter_openscope"])
            names = ["upgrade_menu_power.svg", "upgrade_menu_pinhole.svg"]
        for n in names:
            lbl = QtWidgets.QLabel()
            pm = QtGui.QPixmap(str(PICTURES / n))
            if not pm.isNull():
                lbl.setPixmap(pm.scaledToWidth(260, QtCore.Qt.SmoothTransformation))
            lbl.setToolTip(n)
            self.pics.addWidget(lbl)
            self.pic_labels.append(lbl)
        self.status.setText(labels.WIZ["enter_waiting"])

    def isComplete(self) -> bool:
        w = self.wiz()
        return w is not None and (w.failed or "copy" in w.steps)


class CopyPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Copy")
        self.setSubTitle(labels.WIZ["copy_sub"])
        lay = QtWidgets.QVBoxLayout(self)
        self.warn = _label(labels.WIZ["copy_warn"], bold=True)
        self.warn.setStyleSheet("color:#eb5757; font-weight:bold; font-size:14pt")
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        self.text = _label()
        for wd in (self.warn, self.bar, self.text):
            lay.addWidget(wd)
        lay.addStretch(1)

    def isComplete(self) -> bool:
        w = self.wiz()
        return w is not None and (w.failed or w.finished or "wait" in w.steps)


class RebootPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Restart")
        self.setSubTitle(labels.WIZ["reboot_sub"])
        lay = QtWidgets.QVBoxLayout(self)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        self.text = _label()
        lay.addWidget(self.bar)
        lay.addWidget(self.text)
        lay.addStretch(1)

    def isComplete(self) -> bool:
        w = self.wiz()
        return w is not None and (w.failed or w.finished)


class ResultPage(_Page):
    def __init__(self):
        super().__init__()
        self.setTitle("Result")
        lay = QtWidgets.QVBoxLayout(self)
        self.head = _label(bold=True)
        self.body = QtWidgets.QPlainTextEdit()
        self.body.setReadOnly(True)
        lay.addWidget(self.head)
        lay.addWidget(self.body, 1)
        self.setFinalPage(True)

    def initializePage(self) -> None:
        head, lines = self.wiz().result_text()
        self.head.setText(head)
        self.body.setPlainText("\n".join(lines))

    def isComplete(self) -> bool:
        w = self.wiz()
        return w is not None and (w.finished or w.failed)


# ── the wizard ─────────────────────────────────────────────────────────
class UpdateWizard(QtWidgets.QWizard):
    """Device > Update firmware… (and the banner button). The window passes
    what the worker reported; tests replace the library flow functions."""

    def __init__(self, worker, compat: Optional[labels.Compat] = None, desc=None, fw_version: str = "",
                 parent=None, *, images: Optional[List[Tuple[Path, fw.Manifest]]] = None,
                 run_update: Callable = up.run_update,
                 run_first_install: Callable = up.run_first_install,
                 hooks: Optional[up.Hooks] = None, backup_root=None,
                 check_image: Callable = fw.check_image,
                 choose_file: Optional[Callable[[], Optional[str]]] = None,
                 offline_poll_ms: int = 50):
        super().__init__(parent)
        self.worker = worker
        self.compat, self.desc, self.fw_version = compat, desc, fw_version
        self.images = images if images is not None else find_images(bundled_firmware_dir())
        self._run_update, self._run_first = run_update, run_first_install
        self._hooks, self._root, self._check = hooks, backup_root, check_image
        self._choose = choose_file
        self.image: Optional[Image] = None
        self.path: Optional[str] = path_for(compat)
        self.runner: Optional[UpdateRunner] = None
        self.steps: List[str] = []
        self.gate_step: Optional[str] = None
        self.locked = False             # from the copy until the flow ends: no cancel, no close
        self.finished = False
        self.failed = False
        self.res: Optional[up.UpdateResult] = None
        self.err: Optional[BaseException] = None
        self.last_text = ""
        self.page_times: List[Tuple[str, float]] = []
        self._t_page = time.monotonic()
        self._page_shown = -1
        self._t_copy: Optional[float] = None
        self._offline_poll_ms = offline_poll_ms
        self._pending_start = False

        self.setWindowTitle("Update firmware")
        self.setWizardStyle(QtWidgets.QWizard.ClassicStyle)
        self.setOption(QtWidgets.QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QtWidgets.QWizard.NoBackButtonOnLastPage, True)
        self.setButtonText(QtWidgets.QWizard.CommitButton, "Next >")
        self.pages = [CheckPage(), BackupPage(), EnterPage(), CopyPage(), RebootPage(), ResultPage()]
        for i, p in enumerate(self.pages):
            p.setProperty("page_id", i)
            self.setPage(i, p)
        self.setStartId(CHECK)
        self.currentIdChanged.connect(self._page_changed)
        worker.compat.connect(self.set_compat)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self.resize(680, 520)

    # ── state from the window / worker ─────────────────────────────────
    def set_compat(self, c) -> None:
        """A new compat report while the wizard is on the check page."""
        if self.runner is not None or self._pending_start:
            return
        self.compat = c
        self.path = path_for(c)
        if self.currentId() == CHECK:
            self.pages[CHECK].refresh()

    def set_image(self, path) -> None:
        self.image = Image(Path(path), self._check)

    def choose_file(self) -> Optional[str]:
        if self._choose is not None:
            return self._choose()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose firmware image", str(bundled_firmware_dir() or Path.home()),
            "Firmware image or manifest (*.bin *.manifest.json)")
        return path or None

    # ── the flow ───────────────────────────────────────────────────────
    def start_runner(self) -> None:
        """The worker goes offline first; the runner starts when the port is free."""
        if self.runner is not None or self._pending_start:
            return
        self._pending_start = True
        self.worker.set_online(False)
        self._wait_offline()

    def _wait_offline(self) -> None:
        if not self.worker.is_offline():
            QtCore.QTimer.singleShot(self._offline_poll_ms, self._wait_offline)
            return
        img = self.image
        kw = {"backup_root": self._root, "hooks": self._hooks}
        if self.path == "F":
            fn, args, gates = self._run_first, (img.data, img.manifest, img.name), ()
            kw.update(model=up.MODEL, install_ack=self.pages[BACKUP].typed.text().strip())
        else:
            fn, args, gates = self._run_update, (img.data, img.manifest, img.name), ("enter", "copy")
            kw.update(port=getattr(self.worker, "port", None))
        self.runner = UpdateRunner(fn, args, kw, gates, self)
        self.runner.event.connect(self._event)
        self.runner.gate.connect(self._gate)
        self.runner.result.connect(self._result)
        self.runner.error.connect(self._error)
        self._pending_start = False
        self.runner.start()

    def _gate(self, step: str) -> None:
        self.gate_step = step
        self.pages[BACKUP].result.setText(labels.WIZ["backup_verified"] + self._backup_words())
        self._changed()

    def _backup_words(self) -> str:
        return f"\n{self.last_text}" if self.last_text else ""

    def _event(self, e) -> None:
        if e.kind == "step":
            self.steps.append(e.step)
            if e.step == "copy":
                self._lock()
        if e.text and e.kind in ("info", "wait", "step"):
            self.last_text = e.text
        page = self.currentId()
        if e.kind == "progress" and e.step == "backup" and page == BACKUP:
            self.pages[BACKUP].show_progress(e.done, e.total)
        if e.kind == "wait" and e.step == "enter":
            self.pages[ENTER].status.setText(labels.WIZ["enter_waiting"])
        if page == REBOOT or (page == COPY and e.step != "copy"):
            self.pages[REBOOT].text.setText(e.text)
        if page == BACKUP and e.kind == "info":
            self.pages[BACKUP].result.setText(e.text)
        self._changed()

    def _lock(self) -> None:
        self.locked = True
        self._t_copy = time.monotonic()
        self.setOption(QtWidgets.QWizard.NoCancelButton, True)
        self.button(QtWidgets.QWizard.CancelButton).hide()
        self.button(QtWidgets.QWizard.BackButton).hide()

    def _result(self, res) -> None:
        self.res = res
        self._end()

    def _error(self, e) -> None:
        self.err = e
        self.failed = True
        self._end()

    def _end(self) -> None:
        self.finished = True
        self.locked = False
        self._changed()

    def _changed(self) -> None:
        """Pages that wait for the flow go on by themselves."""
        p = self.currentPage()
        if p is not None:
            p.completeChanged.emit()
        for _ in range(len(self.pages)):
            cid = self.currentId()
            auto = cid in (ENTER, COPY, REBOOT) or (cid in (CHECK, BACKUP) and self.failed)
            if not (auto and self.currentPage().isComplete()):
                break
            self.next()
            if self.currentId() == cid:
                break

    def _page_changed(self, pid: int) -> None:
        """Time on each page (the hand check records them)."""
        now = time.monotonic()
        if 0 <= self._page_shown < len(PAGE_NAMES):
            self.page_times.append((PAGE_NAMES[self._page_shown], round(now - self._t_page, 1)))
        self._page_shown, self._t_page = pid, now
        if pid in (COPY, REBOOT, RESULT) and self.locked:
            self._lock()

    def _tick(self) -> None:
        if self.currentId() == COPY and self._t_copy is not None:
            s = time.monotonic() - self._t_copy
            self.pages[COPY].text.setText(f"{s:.0f} s (a copy takes about {COPY_SECONDS} s)")

    # ── result ─────────────────────────────────────────────────────────
    def result_text(self) -> Tuple[str, List[str]]:
        lines: List[str] = []
        if self.err is not None:
            e = self.err
            if isinstance(e, up.Refused):
                head = labels.WIZ["refused"]
            elif isinstance(e, Cancelled):
                head = labels.WIZ["cancelled"]
            elif isinstance(e, up.UpdateError) and e.copied:
                head = labels.WIZ["failed_copied"]
            else:
                head = labels.WIZ["failed_before"]
            lines.append(str(e))
            log = getattr(e, "log", None)
            if log:
                lines.append(f"Update log: {log}")
        elif self.res is not None:
            r = self.res
            head = labels.WIZ["ok"] if r.ok else labels.WIZ["problems"]
            lines.append("Identity: the unit runs the image of the manifest." if r.ok else
                         "Differences: " + "; ".join(r.problems))
            lines += ["Calibration: " + x for x in r.compare_lines]
            if r.before_backup:
                lines.append(f"Backup before: {r.before_backup}")
            if r.after_backup:
                lines.append(f"Backup after: {r.after_backup}")
            if r.log:
                lines.append(f"Update log: {r.log}")
            lines.append(f"Time: {r.seconds:.0f} s, of which {r.user_wait_seconds:.0f} s waiting for you")
        else:
            head = labels.WIZ["cancelled"]
        shown = list(self.page_times)
        if 0 <= self._page_shown < RESULT:      # Qt calls initializePage before currentIdChanged
            shown.append((PAGE_NAMES[self._page_shown], time.monotonic() - self._t_page))
        times = ", ".join(f"{n} {s:.0f} s" for n, s in shown)
        if times:
            lines.append(f"Pages: {times}")
        return head, lines

    # ── closing ────────────────────────────────────────────────────────
    def done(self, r: int) -> None:
        """No close while the flow writes or waits after the copy. A cancel
        before the copy stops the flow (nothing is written). Every close puts
        the worker online again; it reads the description again (stage-180)."""
        if self.locked:
            return
        if self.runner is not None and self.runner.isRunning():
            self.runner.cancel()
            self.runner.wait(20000)
        self.timer.stop()
        try:
            self.worker.compat.disconnect(self.set_compat)
        except (RuntimeError, TypeError):
            pass
        self.worker.set_online(True)
        super().done(r)
