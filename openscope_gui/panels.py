"""Control panel structure (stage-260): one interface for the panels of all
unit modes, and one helper for "send when editing is finished, show the state
the unit reports".

* `Panel`: the base of every control panel. The window calls only these
  methods, for every panel: `set_description(d)`, `set_state(obj)`, `clear()`,
  `set_enabled(on)`. A state update never sends a set (stage-085 rule).
* `EditSender`: connects one widget to one send call. A spin box or line edit
  sends on "editing finished" (Enter or focus out), a combo box on `activated`
  (user only). An edit is sent once: Enter and then focus out with the same
  value send one set, and a value equal to the last value sent or reported
  sends nothing.
* `show_reported(widget, value)`: writes a reported value into a widget only
  when the widget has no focus and the value changed, with signals blocked, so
  a state update never overwrites text being typed and never sends (code review
  2026-11-23 Q-1).
* `PlaceholderPanel`: one line of text, for a mode whose controls come later
  (Multimeter stage-275, Signal generator stage-285).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6 import QtCore, QtWidgets


def widget_value(w: QtWidgets.QWidget) -> Any:
    """The value a widget shows: combo data, spin box value, or edit text."""
    if isinstance(w, QtWidgets.QComboBox):
        return w.currentData()
    if isinstance(w, QtWidgets.QAbstractSpinBox):
        return w.value()
    if isinstance(w, QtWidgets.QLineEdit):
        return w.text()
    raise TypeError(f"no value for {type(w).__name__}")


def write_value(w: QtWidgets.QWidget, value: Any) -> bool:
    """Put `value` in the widget with signals blocked, only when it changed
    (each Qt setter writes the edit text again). True if it wrote."""
    if isinstance(w, QtWidgets.QDoubleSpinBox):
        value = round(value, w.decimals())      # the value the box keeps
    if widget_value(w) == value:
        return False
    w.blockSignals(True)
    try:
        if isinstance(w, QtWidgets.QComboBox):
            w.setCurrentIndex(w.findData(value))
        elif isinstance(w, QtWidgets.QAbstractSpinBox):
            w.setValue(value)
        else:
            w.setText(value)
    finally:
        w.blockSignals(False)
    return True


def show_reported(w: QtWidgets.QWidget, value: Any) -> bool:
    """Show a value the unit reported: only when the widget has no focus and
    the value changed; signals blocked, so it never sends. True if it wrote."""
    if w.hasFocus():
        return False
    return write_value(w, value)


class EditSender(QtCore.QObject):
    """One widget, one send call.

    `send(v)` gets `value()` (default: the widget's value) once per finished
    user edit whose value is not the last value sent or reported. `reported(v)`
    tells it the value in force on the unit (from a state update), so after a
    refusal the same edit can be sent again. `finished(v)`, if given, runs after
    every finished edit, sent or not (e.g. to show the value on a new scale).
    A value of None is never sent."""

    def __init__(self, widget: QtWidgets.QWidget, send: Callable[[Any], None], *,
                 value: Optional[Callable[[], Any]] = None,
                 finished: Optional[Callable[[Any], None]] = None):
        super().__init__(widget)
        self.widget = widget
        self._send = send
        self._value = value if value is not None else (lambda: widget_value(widget))
        self._finished = finished
        self.last: Any = None
        self.sent = 0
        if isinstance(widget, QtWidgets.QComboBox):
            widget.activated.connect(self._edited)
        else:
            widget.editingFinished.connect(self._edited)

    def reported(self, value: Any) -> None:
        self.last = value

    def show_reported(self, value: Any, shown: Any = None) -> bool:
        """`value` is in force on the unit; show `shown` (default `value`) in the
        widget unless it has the focus. True if it wrote."""
        self.reported(value)
        return show_reported(self.widget, value if shown is None else shown)

    def _edited(self, *_) -> None:
        v = self._value()
        if v is not None and v != self.last:
            self.last = v
            self.sent += 1
            self._send(v)
        if self._finished is not None:
            self._finished(v)


class Panel(QtWidgets.QWidget):
    """Base of the control panels; `mode` is the unit mode id of its tab."""
    mode: int = -1

    def set_description(self, d) -> None:
        """The device description at connect (None: firmware without it)."""

    def set_state(self, s) -> None:
        """The state the unit reports for this mode. Sends nothing."""

    def clear(self) -> None:
        """No unit: show nothing from the last connection."""
        self.set_enabled(False)

    def set_enabled(self, on: bool) -> None:
        self.setEnabled(on)


class PlaceholderPanel(Panel):
    def __init__(self, mode: int, text: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        lay = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        lay.addStretch(1)

    def clear(self) -> None:
        """Nothing to clear: the text stays readable."""

    def text(self) -> str:
        return self.label.text()
