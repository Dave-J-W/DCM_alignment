"""
DCM Alignment Console — PyQt6 desktop application.

Tabs:
  1. Setup        — PV names, scan parameters, connection test
  2. Energy Table — Editable lookup table (MonoE, UE, Roll, Pitch)
  3. Alignment    — Step-by-step alignment runner with live scan plots
  4. Mirror       — Placeholder for mirror alignment substeps

Run:
    python dcm_align_app.py

Optional EPICS support (pyepics):
    pip install pyepics
    Uncheck "Simulation mode" in Setup to connect to real hardware.
"""

import sys
import time
import bisect
import csv
import json
import os
import random
import socket
import tempfile
import threading
import concurrent.futures
from datetime import datetime
import codecs
import html

# pyepics on Windows passes 'utf-8:surrogatescape' as a codec name, which Python rejects.
# codecs.lookup normalises '-' → '_' before calling search functions, so the name
# arrives as 'utf_8:surrogatescape'. Register an alias so epics can initialise correctly.
try:
    codecs.lookup('utf-8:surrogatescape')
except LookupError:
    codecs.register(lambda name: codecs.lookup('utf-8') if 'surrogateescape' in name else None)

import numpy as np
from scipy.optimize import curve_fit

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QTabWidget, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit,
    QGroupBox, QCheckBox, QSpinBox, QDoubleSpinBox, QComboBox,
    QSplitter, QFrame, QFileDialog, QMessageBox, QProgressBar,
    QAbstractItemView, QScrollArea, QStatusBar, QDialog,
    QStyle, QStyleOptionHeader,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QObject, QRect,
)
from PyQt6.QtGui import QFont, QColor, QPainter, QPen, QFontMetrics

import pyqtgraph as pg

# ─── Try importing pyepics; fall back to simulation ──────────────────────────
try:
    import epics
    EPICS_AVAILABLE = True
except ImportError:
    EPICS_AVAILABLE = False

# ─── Colour palettes ─────────────────────────────────────────────────────────
THEMES = {
    "Ocean Light": {
        "bg": "#f5f7fa", "surface": "#eaeef2", "surface_hi": "#dde2e8",
        "border": "#c8d0d8", "cyan": "#0a7a82", "cyan_dim": "#c8eef0",
        "green": "#1a7f37", "amber": "#9a6700", "red": "#cf2218",
        "text_pri": "#1f2328", "text_sec": "#57606a", "text_dim": "#6e7781",
        "tag_green_bg": "#e6f4ea", "tag_amber_bg": "#fff3cd", "tag_red_bg": "#fce8e6",
    },
    "Midnight": {
        "bg": "#0d1117", "surface": "#161b22", "surface_hi": "#21262d",
        "border": "#30363d", "cyan": "#2dd4d9", "cyan_dim": "#0c2e30",
        "green": "#3fb950", "amber": "#e3b341", "red": "#f85149",
        "text_pri": "#e6edf3", "text_sec": "#8b949e", "text_dim": "#6e7781",
        "tag_green_bg": "#1a3224", "tag_amber_bg": "#2d2008", "tag_red_bg": "#2d1618",
    },
    "Slate Blue": {
        "bg": "#f1f5f9", "surface": "#e2e8f0", "surface_hi": "#cbd5e1",
        "border": "#94a3b8", "cyan": "#2563eb", "cyan_dim": "#dbeafe",
        "green": "#16a34a", "amber": "#d97706", "red": "#dc2626",
        "text_pri": "#0f172a", "text_sec": "#334155", "text_dim": "#64748b",
        "tag_green_bg": "#dcfce7", "tag_amber_bg": "#fef3c7", "tag_red_bg": "#fee2e2",
    },
    "Carbon": {
        "bg": "#1c1c1e", "surface": "#2c2c2e", "surface_hi": "#3a3a3c",
        "border": "#48484a", "cyan": "#f5c542", "cyan_dim": "#3a2d00",
        "green": "#30d158", "amber": "#ff9f0a", "red": "#ff453a",
        "text_pri": "#f2f2f7", "text_sec": "#aeaeb2", "text_dim": "#636366",
        "tag_green_bg": "#1a3824", "tag_amber_bg": "#3a2800", "tag_red_bg": "#380c0a",
    },
}
PAL = dict(THEMES["Ocean Light"])

# Trace colours for scan plots. Deliberately NOT drawn from PAL: a trace's
# colour is its identity in the legend and must not change meaning when the
# operator switches theme (PAL["cyan"] is teal in Ocean Light, blue in Slate
# Blue and yellow in Carbon). Each of these reads against both the lightest
# app background (#f5f7fa) and the darkest (#0d1117).
SERIES_COLORS = [
    "#3b82f6",   # blue
    "#e8590c",   # orange
    "#099268",   # green
    "#ae3ec9",   # purple
    "#1098ad",   # teal
    "#e64980",   # pink
]


def build_qss(pal):
    return f"""
QMainWindow, QDialog {{
    background: {pal['bg']};
}}
QWidget {{
    background: {pal['bg']};
    color: {pal['text_pri']};
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 12px;
}}
QTabWidget::pane {{
    border: 1px solid {pal['border']};
    background: {pal['surface']};
}}
QTabBar::tab {{
    background: {pal['surface']};
    color: {pal['text_sec']};
    padding: 8px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
    font-size: 12px;
}}
QTabBar::tab:selected {{
    color: {pal['cyan']};
    border-bottom: 2px solid {pal['cyan']};
    background: {pal['surface']};
}}
QGroupBox {{
    border: 1px solid {pal['border']};
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px;
    background: {pal['surface']};
    font-weight: 600;
    color: {pal['text_sec']};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QLineEdit {{
    background: {pal['bg']};
    border: 1px solid {pal['border']};
    border-radius: 4px;
    padding: 5px 8px;
    color: {pal['cyan']};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 11px;
    selection-background-color: {pal['cyan_dim']};
}}
QLineEdit:focus {{
    border: 1px solid {pal['cyan']};
}}
QDoubleSpinBox, QSpinBox {{
    background: {pal['bg']};
    border: 1px solid {pal['border']};
    border-radius: 4px;
    padding: 4px 8px;
    color: {pal['cyan']};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
}}
QDoubleSpinBox:focus, QSpinBox:focus {{
    border: 1px solid {pal['cyan']};
}}
QComboBox {{
    background: {pal['bg']};
    border: 1px solid {pal['border']};
    border-radius: 4px;
    padding: 4px 8px;
    color: {pal['text_pri']};
}}
QComboBox::drop-down {{
    border: none;
}}
QPushButton {{
    background: {pal['bg']};
    border: 1px solid {pal['border']};
    border-radius: 5px;
    padding: 6px 16px;
    color: {pal['text_sec']};
    font-weight: 600;
    font-size: 12px;
}}
QPushButton:hover {{
    background: {pal['bg']};
    border-color: {pal['cyan']};
    color: {pal['cyan']};
}}
QPushButton:disabled {{
    background: {pal['surface']};
    color: {pal['border']};
    border-color: {pal['surface_hi']};
}}
QPushButton#primary {{
    background: {pal['cyan_dim']};
    color: {pal['text_pri']};
    border: 2px solid {pal['cyan']};
    padding: 7px 20px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#primary:hover {{
    background: {pal['cyan']};
    color: {pal['text_pri']};
    border-color: {pal['cyan']};
}}
QPushButton#primary:disabled {{
    background: {pal['surface_hi']};
    color: {pal['text_dim']};
    border-color: {pal['border']};
}}
QPushButton#danger {{
    border-color: {pal['red']};
    color: {pal['red']};
}}
QPushButton#danger:hover {{
    background: {pal['tag_red_bg']};
}}
QTableWidget {{
    background: {pal['surface']};
    alternate-background-color: {pal['surface_hi']};
    border: 1px solid {pal['border']};
    border-radius: 4px;
    gridline-color: {pal['border']};
    selection-background-color: {pal['cyan_dim']};
    selection-color: {pal['cyan']};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
}}
QHeaderView::section {{
    background: {pal['surface_hi']};
    color: {pal['text_dim']};
    padding: 6px 10px;
    border: none;
    border-bottom: 1px solid {pal['border']};
    font-weight: 600;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QTextEdit {{
    background: {pal['bg']};
    border: 1px solid {pal['border']};
    border-radius: 4px;
    color: {pal['text_sec']};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 11px;
    padding: 6px;
}}
QScrollBar:vertical {{
    background: {pal['bg']};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {pal['border']};
    border-radius: 4px;
    min-height: 20px;
}}
QCheckBox {{
    color: {pal['text_sec']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {pal['border']};
    border-radius: 3px;
    background: {pal['bg']};
}}
QCheckBox::indicator:checked {{
    background: {pal['cyan']};
    border-color: {pal['cyan']};
}}
QProgressBar {{
    background: {pal['surface_hi']};
    border: 1px solid {pal['border']};
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {pal['cyan']};
    border-radius: 3px;
}}
QStatusBar {{
    background: {pal['surface']};
    color: {pal['text_dim']};
    font-size: 11px;
    border-top: 1px solid {pal['border']};
}}
QLabel#readout_value {{
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 20px;
    font-weight: 700;
    color: {pal['cyan']};
}}
QLabel#readout_label {{
    font-size: 10px;
    color: {pal['text_dim']};
    letter-spacing: 1px;
    text-transform: uppercase;
}}
QLabel#step_title {{
    font-size: 13px;
    font-weight: 600;
    color: {pal['text_pri']};
}}
QLabel#tag_green {{
    background: {pal['tag_green_bg']};
    border: 1px solid {pal['green']};
    color: {pal['green']};
    border-radius: 3px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#tag_amber {{
    background: {pal['tag_amber_bg']};
    border: 1px solid {pal['amber']};
    color: {pal['amber']};
    border-radius: 3px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#tag_red {{
    background: {pal['tag_red_bg']};
    border: 1px solid {pal['red']};
    color: {pal['red']};
    border-radius: 3px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#tag_grey {{
    background: {pal['surface_hi']};
    border: 1px solid {pal['border']};
    color: {pal['text_dim']};
    border-radius: 3px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#tag_cyan {{
    background: {pal['cyan_dim']};
    border: 1px solid {pal['cyan']};
    color: {pal['cyan']};
    border-radius: 3px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 10px;
    font-weight: 700;
}}
"""


QSS = build_qss(PAL)

AUTO_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dcm_config.json")
RUN_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dcm_run.lock")


# ─── Config file helpers: atomic writes, and a cross-app run lock ────────────
# Both the full console and the mini console (dcm_mini_app.py) read and write
# dcm_config.json, and both must be able to start a run that touches real
# hardware without racing the other. These are module-level, not methods, so
# either app can `import dcm_align_app as app` and call `app.<name>` without
# needing a MainWindow instance.

def _pid_is_running(pid):
    """Best-effort liveness check for a process id, Windows and POSIX.

    There is no working os.kill(pid, 0) on Windows: unlike POSIX, where
    signal 0 means "just check, don't deliver", Windows os.kill only accepts
    SIGTERM and the two CTRL_*_EVENT values -- passing 0 raises OSError
    rather than probing anything. Rather than pull in psutil as a new
    dependency, this opens a handle to the process through the raw Win32 API
    via ctypes (stdlib, nothing to install) and treats "handle obtained" and
    "access denied" both as running, anything else as not running.

    Stated limitation: PIDs are recycled by the OS, so given enough churn (or
    a reboot) a dead process's old PID can belong to an unrelated live
    process, which would make this wrongly report "running". That is a
    plausibility check for whether to honour a lock file, not a proof of
    identity -- an acceptable trade here, since the failure mode is an
    operator seeing one extra "already running" refusal, not a silent
    double-run on real hardware.
    """
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def atomic_write_json(path, obj):
    """Write obj as JSON (indent=2) via a temp file in the same directory,
    then os.replace() onto the target. Returns (ok: bool, reason: str).

    os.replace() is atomic on both POSIX and Windows -- unlike os.rename(),
    which raises on Windows when the destination already exists -- so a
    reader never observes a partially written file, and a crash or a second
    writer mid-write leaves the previous good file in place rather than
    truncated JSON.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".dcm_tmp_", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2)
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, "ok"


def load_config_file(path=None):
    """Read the whole config JSON and return it as a dict.

    Defaults to the CURRENT value of the module-level AUTO_CONFIG_PATH,
    looked up at call time rather than captured as a default-argument value
    -- tests/_harness.py rebinds app.AUTO_CONFIG_PATH for test isolation, and
    a default argument would freeze the path from before that rebind, which
    is exactly the isolation mechanism this has to respect. Returns {} if the
    file is absent, unparseable, or not a JSON object; never raises. Callers
    that need to distinguish "no file" from "empty config" should check
    os.path.exists() themselves first.
    """
    p = path or AUTO_CONFIG_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config_section(section, data, path=None):
    """Set cfg[section] = data in the config file and write it back atomically.

    Re-reads the file immediately before writing, rather than working from a
    snapshot taken earlier, so every other top-level key -- including one
    owned by a different process sharing the same file -- survives untouched.
    A stashed-at-load-time snapshot would let this silently overwrite a
    section a second app wrote more recently than this process's own load.
    Returns (ok, reason).
    """
    p = path or AUTO_CONFIG_PATH
    cfg = load_config_file(p)
    cfg[section] = data
    return atomic_write_json(p, cfg)


def read_run_lock(path=None):
    """Return the current run lock as a dict, or None if there is none.

    The dict carries pid, host, app, started (an ISO 8601 string) as written
    by acquire_run_lock(), plus a bool 'stale' that is True when that pid is
    no longer running (see _pid_is_running() for what that check can and
    cannot prove). Returns None when the lock file is absent or unparseable
    -- an unreadable lock is treated the same as no lock, since acquiring one
    would overwrite it regardless.
    """
    p = path or RUN_LOCK_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = dict(data)
    result["stale"] = not _pid_is_running(data.get("pid"))
    return result


def acquire_run_lock(app_name, path=None):
    """Claim the run lock for this process. Returns (ok, holder).

    holder is None when ok is True. When ok is False, holder is the blocking
    lock dict (see read_run_lock()) belonging to whoever holds it. A lock
    whose pid is no longer running is stale and is overwritten here rather
    than honoured -- its own process is gone, so nothing else would ever
    clear it. (Callers that want to ask the operator before overriding a
    stale lock should read_run_lock() first and only call this after they
    decide to proceed; this function itself does not ask.) Re-acquiring a
    lock this same process already holds succeeds and refreshes 'started'.
    """
    p = path or RUN_LOCK_PATH
    existing = read_run_lock(p)
    if (existing is not None and not existing.get("stale")
            and existing.get("pid") != os.getpid()):
        return False, existing
    lock = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "app": app_name,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    ok, reason = atomic_write_json(p, lock)
    if not ok:
        # Could not even write the lock file -- treat that as "the lock could
        # not be claimed" rather than silently letting the run proceed
        # unlocked, which would defeat the whole point of having one.
        return False, {"pid": None, "host": "", "app": app_name, "started": "",
                       "stale": False, "error": reason}
    return True, None


def release_run_lock(path=None):
    """Remove the run lock if, and only if, this process owns it. Never raises."""
    p = path or RUN_LOCK_PATH
    try:
        lock = read_run_lock(p)
        if lock is None or lock.get("pid") != os.getpid():
            return
        os.remove(p)
    except Exception:
        pass


MOTOR_PV_KEYS = {"mono_energy", "roll", "pitch", "mir_slit_top", "mir_slit_bot",
                 "mir_pitch_motor"}

# Which settings tab owns which PV and which scan parameter. The three panels
# are the same widget class with different filters, so the CA monitoring,
# readbacks and config handling exist once. Anything not listed here is not
# shown at all, which is how a key gets retired.
PV_TABS = {
    "global": ["und_energy", "und_harmonic", "und_start", "und_busy", "und_energy_rbv",
               "bpm_x", "bpm_y", "bpm_intensity", "bpm_sen",
               "feedback_h", "feedback_v", "auto_feedback", "shutter_blocking",
               "ion_chamber", "ic_sen_unit", "ic_sen_num"],
    "dcm":    ["mono_energy", "roll", "pitch", "piezo_pitch", "piezo_roll"],
    "mirror": ["mir_slit_top", "mir_slit_bot", "mir_pitch_motor", "mir_piezo_pitch",
               "mir_slit_center", "mir_slit_size"],
}
SCAN_TABS = {
    # Shared infrastructure and anything both devices read.
    "global": ["settle_time", "piezo_settle_time", "pv_ack_timeout",
               "motor_start_grace", "motor_stall_timeout"],
    "dcm":    ["dcm_signal", "pitch_start", "pitch_stop", "pitch_steps",
               "roll_start", "roll_stop", "roll_steps", "piezo_center",
               "dcm_piezo_start", "dcm_piezo_stop", "dcm_piezo_steps",
               "smart_edge_fraction", "smart_max_extend_steps",
               "smart_fine_sigma_range", "smart_fine_scan_iter"],
    "mirror": ["mir_piezo_start", "mir_piezo_stop", "mir_piezo_steps"],
}

# ─── Default config ───────────────────────────────────────────────────────────
DEFAULT_PVS = {
    "mono_energy":      "DCM:mono:Energy",
    "und_energy":       "DCM:und:Energy",
    "roll":             "DCM:roll:SP",
    "pitch":            "DCM:pitch:SP",
    "piezo_pitch":      "DCM:piezo:pitch:SP",
    "piezo_roll":       "DCM:piezo:roll:SP",
    "bpm_x":            "BPM:x:readback",
    "bpm_y":            "BPM:y:readback",
    "bpm_intensity":    "BPM:intensity:readback",
    "feedback_h":       "BPM:feedback:H:enable",
    "feedback_v":       "BPM:feedback:V:enable",
    # 15IDA:userTran6 drives the feedback loops as .G=(a||o)&&... / .H=(b||o)&&...
    # so while .O ("AutoFeedback") is 1 it forces both loops on and every write
    # to feedback_h/feedback_v below is ignored. Cleared at each chapter start.
    "auto_feedback":    "15IDA:userTran6.O",
    # PSS shutter status, read only. A bi record with ZNAM="OFF"/ONAM="ON":
    # 0 means nothing is blocking the beam. Note it sits at MAJOR severity even
    # in that good state, so severity is not a usable test here.
    "shutter_blocking": "S15ID-PSS:SCS:BeamBlockingM",
    "und_harmonic":     "",
    "und_start":        "",
    "und_busy":         "S15ID:USID:BusyM",
    "und_energy_rbv":   "S15ID:USID:EnergyM",
    "mir_slit_top":     "15IDA:m9",
    "mir_slit_bot":     "15IDA:m10",
    "mir_piezo_pitch":  "ID15A1:DMS:VDM:FIPI:DCOM",
    "mir_pitch_motor":  "ID15A1:DMS:VDM:PI",
    "ion_chamber":      "",
    "mir_slit_center":  "",
    "mir_slit_size":    "",
    "bpm_sen":          "15ID:FX4_1:Range",
    "ic_sen_unit":      "15IDC:A2sens_unit.VAL",
    "ic_sen_num":       "15IDC:A2sens_num.VAL",
}

_ENERGY_NUMERIC = (("mono_e", float, 0.0), ("ue", float, 0.0),
                   ("harmonic", int, 1), ("roll", float, 0.0),
                   ("pitch", float, 0.0))
_ENERGY_STRINGS = {"bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"}


def coerce_energy_row(row):
    """Normalise an energy-table row to the types the sequence assumes.

    Rows arrive from JSON, from CSV and from hand-edited table cells. A string
    that survives in this dict is later written straight to a motor.
    """
    out = dict(row)
    for key, cast, default in _ENERGY_NUMERIC:
        try:
            out[key] = cast(float(row.get(key, default)))
        except (TypeError, ValueError):
            out[key] = cast(default)
    for key, default in _ENERGY_STRINGS.items():
        val = row.get(key, default)
        out[key] = default if val is None else str(val)
    return out


def coerce_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def set_spin_value(widget, value):
    """Assign a config value to a spin box without raising. Returns success.

    A float landing in a QSpinBox, or any string, used to raise TypeError; in
    _auto_load_config a bare `except` swallowed it, so the remainder of the
    config was silently dropped.
    """
    try:
        if isinstance(widget, QSpinBox):
            widget.setValue(int(round(float(value))))
        else:
            widget.setValue(float(value))
        return True
    except (TypeError, ValueError):
        return False


def fmt_pv_value(val):
    """Format a caget result for the record table.

    isinstance(val, (int, float)) misses numpy integer scalars and arrays, so
    those used to fall through to str() and land in the CSV as "[1. 2. 3.]".
    """
    if val is None:
        return "\u2014"
    if isinstance(val, np.ndarray):
        if val.size == 1:
            return fmt_pv_value(val.item())
        return np.array2string(val, precision=6, threshold=8, separator=",")
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    if isinstance(val, (bool, str)):
        return str(val)
    try:
        return "%.6g" % float(val)
    except (TypeError, ValueError):
        return str(val)


def fmt_xtal(val):
    """DCM crystal-set readback -> '111'/'311'. Tolerates strings and None."""
    if val is None:
        return "\u2014"
    try:
        return {0: "111", 1: "311"}.get(int(float(val)), str(val))
    except (TypeError, ValueError):
        return str(val)


def calc_harmonic(mono_e):
    if mono_e < 13:
        return 1
    elif mono_e < 28:
        return 3
    return 5

DEFAULT_LOOKUP = [
    {"mono_e": 8.0,  "ue": 9.8,  "roll": 0.412, "pitch": 2.341, "harmonic": 1, "bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"},
    {"mono_e": 10.0, "ue": 12.1, "roll": 0.398, "pitch": 2.187, "harmonic": 1, "bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"},
    {"mono_e": 12.0, "ue": 14.6, "roll": 0.381, "pitch": 2.054, "harmonic": 1, "bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"},
    {"mono_e": 15.0, "ue": 18.2, "roll": 0.362, "pitch": 1.893, "harmonic": 3, "bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"},
    {"mono_e": 20.0, "ue": 24.1, "roll": 0.344, "pitch": 1.712, "harmonic": 3, "bpm_sen": "1", "ic_sen_unit": "2", "ic_sen_num": "2"},
]

DEFAULT_RECORD_PVS = [
    # ── Core motor / beam settings ──
    {"label": "Mono Energy (keV)",              "pv": "ID15A1:DCMM:XTAL:E.RBV",                   "checked": True,  "locked": True},
    {"label": "XTAL",                           "pv": "ID15A1:DCMM:BLMODE:ACS:RET_RBV",            "checked": True,  "locked": True},
    {"label": "Undulator Energy (keV)",         "pv": "S15ID:USID:EnergyM.VAL",                    "checked": True,  "locked": True},
    {"label": "Undulator Gap (mm)",             "pv": "S15ID:USID:GapM.VAL",                       "checked": True,  "locked": True},
    {"label": "Undulator Harmonic",             "pv": "S15ID:USID:HarmonicValueC",                 "checked": True,  "locked": True},
    {"label": "Ring Current (mA)",              "pv": "S-DCCT:CurrentM",                           "checked": True,  "locked": True},
    {"label": "DCM Pitch Motor (µrad)",         "pv": "ID15A1:DCMM:XTAL:PI2.RBV",                 "checked": True,  "locked": True},
    {"label": "DCM Pitch Encoder",              "pv": "ID15A1:DCMM:XTAL:FIPI2",                    "checked": True,  "locked": True},
    {"label": "DCM Roll Motor (µrad)",          "pv": "ID15A1:DCMM:XTAL:RO2.RBV",                 "checked": True,  "locked": True},
    {"label": "DCM Roll Encoder",               "pv": "ID15A1:DCMM:XTAL:FIRO2.RBV",               "checked": True,  "locked": True},
    {"label": "Mirror Slit Center (mm)",        "pv": "15IDA:MirVt2.D",                            "checked": True,  "locked": True},
    {"label": "Mirror Angle (µrad)",            "pv": "ID15A1:DMS:VFM:PI.RBV",                     "checked": True,  "locked": True},
    {"label": "Mirror 1st Y (µm)",              "pv": "ID15A1:DMS:VFM:PI.RBV",                     "checked": True,  "locked": True},
    {"label": "Mirror 2nd Y (µm)",              "pv": "ID15A1:DMS:VDM:PI.RBV",                     "checked": True,  "locked": True},
    {"label": "BPM Max Intensity",              "pv": "15IDC:userTran10.E",                        "checked": True,  "locked": True},
    {"label": "BPM Sensitivity",                "pv": "15ID:FX4_1:Range_RBV",                      "checked": True,  "locked": True},
    {"label": "MonP Max Intensity",             "pv": "15IDC:scaler1.S3",                          "checked": True,  "locked": True},
    {"label": "MonP Sensitivity Unit",          "pv": "15IDC:A2sens_unit.VAL",                     "checked": True,  "locked": True},
    {"label": "MonP Sensitivity Num",           "pv": "15IDC:A2sens_num.VAL",                      "checked": True,  "locked": True},
    # ── Computed during alignment sequence ──
    {"label": "BPM Max Intensity w/o Mirror",   "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    {"label": "MonP Max Intensity w/o Mirror",  "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    {"label": "Mirror Stripe",                  "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    {"label": "BPM Y @ 3D (µm)",                 "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    {"label": "VDM Y FWHM @ 4D (µm)",          "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    {"label": "VFM Y FWHM @ 4E (µm)",          "pv": "",  "checked": True,  "locked": True, "source": "scan_result"},
    # ── Optional / unchecked by default ──
    {"label": "2nd Xtal Temp (°C)",             "pv": "15ID:BLEPS:TEMP23_CURRENT",                 "checked": False, "locked": True},
    {"label": "RF BPM Vertical (µrad)",         "pv": "S15:ID:SrcPt:VAngleM",                      "checked": False, "locked": True},
    {"label": "RF BPM Horizontal (µrad)",       "pv": "S15:ID:SrcPt:HAngleM",                      "checked": False, "locked": True},
    {"label": "XBPM US X (µm)",                 "pv": "S15IDFE-XBPM:P1us:x:LowPass1s_DecimatedM", "checked": False, "locked": True},
    {"label": "XBPM US Y (µm)",                 "pv": "S15IDFE-XBPM:P1us:y:LowPass1s_DecimatedM", "checked": False, "locked": True},
    {"label": "XBPM DS X (µm)",                 "pv": "S15IDFE-XBPM:P1ds:x:LowPass1s_DecimatedM", "checked": False, "locked": True},
    {"label": "XBPM DS Y (µm)",                 "pv": "S15IDFE-XBPM:P1ds:y:LowPass1s_DecimatedM", "checked": False, "locked": True},
]

DEFAULT_MIRROR_STAGES = [
    {"name": "JJC Center",           "pv": "15IDC:Slit4VDcenter.VAL",  "val_in":  0.0,    "val_out": -2.0},
    # JJC Size stays open at 4 for the whole alignment. The 0.4 in
    # mirror_in_out.docx is the post-alignment value and must not be used here.
    {"name": "JJC Size",             "pv": "15IDC:Slit4VDsize.VAL",    "val_in":  4.0,    "val_out":  4.0},
    {"name": "CRL Y",                "pv": "15IDMini:m2",              "val_in":  2.0,    "val_out":  0.0},
    {"name": "VFM Y",                "pv": "ID15A1:DMS:VFM:Y",         "val_in":  0.0,    "val_out": -3000.0},
    {"name": "VDM Y",                "pv": "ID15A1:DMS:VDM:Y",         "val_in":  0.0,    "val_out":  3000.0},
    {"name": "BPM Y",                "pv": "15IDC:m3",                 "val_in":  0.0,    "val_out": -2.0},
    {"name": "BPM Scale Y",          "pv": "15IDA:userTran10.CLCI",    "val_in":  51.2,   "val_out":  512.0},
    {"name": "Vertical Feedback P",  "pv": "15ID1:BeamPosY.KP",        "val_in":  0.0001, "val_out":  0.001},
]

DEFAULT_SCAN = {
    "dcm_signal": "BPM Intensity",
    "pitch_start": -0.05,
    "pitch_stop":   0.05,
    "pitch_steps":  25,
    "roll_start":  -0.05,
    "roll_stop":    0.05,
    "roll_steps":   21,
    "settle_time":        0.1,
    "piezo_settle_time":  0.2,
    "pv_ack_timeout":       10.0,
    "motor_start_grace":     5.0,
    "motor_stall_timeout":  30.0,
    "piezo_center": 5.0,
    "smart_edge_fraction":    0.2,
    "smart_max_extend_steps": 10,
    "smart_fine_sigma_range": 2.0,
    "smart_fine_scan_iter":   3,
    "dcm_piezo_start":  -1.0,
    "dcm_piezo_stop":    1.0,
    "dcm_piezo_steps":  21,
    "mir_piezo_start":  -1.0,
    "mir_piezo_stop":    1.0,
    "mir_piezo_steps":  21,
}

DEFAULT_MIRROR_SCAN = {
    "mir_signal":         "BPM Intensity",
    "mir_slit_size_a":    0.1,
    "mir_slit_cen_start": -2.0,
    "mir_slit_cen_stop":  2.0,
    "mir_slit_cen_steps": 21,
    "mir_slit_size_b":    0.2,
    "mir_vdm_start":      -500.0,
    "mir_vdm_stop":        500.0,
    "mir_vdm_steps":       21,
    "mir_vfm_start":      -250.0,
    "mir_vfm_stop":        250.0,
    "mir_vfm_steps":       21,
    "mir_slit_size_c":    1.0,
    "jjc_size_pre_feedback": 0.4,
    # 4C now steps the VDM pitch motor (urad), not the piezo (DCOM ~5), so it
    # needs its own range rather than reusing mir_piezo_*.
    "mir_pitch_start":    -50.0,
    "mir_pitch_stop":      50.0,
    "mir_pitch_steps":     21,
}

# ─── Mirror stripe selection ─────────────────────────────────────────────────
_STRIPE_VFM_X_PV = "ID15A1:DMS:VFM:X"
_STRIPE_VDM_X_PV = "ID15A1:DMS:VDM:X"
_STRIPE_POSITIONS = {
    "Si": {"vfm_x":      0, "vdm_x":      0},
    "Rh": {"vfm_x":  13000, "vdm_x": -13000},
    "Pt": {"vfm_x": -13000, "vdm_x":  13000},
}

def select_stripe(energy_kev):
    if energy_kev <= 12:
        return "Si"
    elif energy_kev <= 23:
        return "Rh"
    else:
        return "Pt"

# ─── Simulation helpers ───────────────────────────────────────────────────────
def gaussian(x, center, sigma, amp, offset=0.0):
    return amp * np.exp(-0.5 * ((x - center) / sigma) ** 2) + offset

def super_gaussian(x, amplitude, center, sigma, p, offset):
    return amplitude * np.exp(-np.abs((x - center) / sigma) ** p) + offset

def fit_super_gaussian(xs, ys):
    """Multi-start super-Gaussian fit (p∈{2,4,8,16}). Returns [amp,center,sigma,p,offset] or None."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    if len(xs) < 5:
        return None
    amp0 = float(np.max(ys) - np.min(ys))
    cen0 = float(xs[np.argmax(ys)])
    sig0 = float(max((xs[-1] - xs[0]) / 4.0, 1e-9))
    off0 = float(np.min(ys))
    best, best_rms = None, np.inf
    for p_init in [2.0, 4.0, 8.0, 16.0]:
        try:
            lo = [0.0, xs[0], 1e-9, 0.5, -np.inf]
            hi = [np.inf, xs[-1], xs[-1] - xs[0] + 1e-9, 64.0, np.inf]
            popt, _ = curve_fit(super_gaussian, xs, ys,
                                p0=[amp0, cen0, sig0, p_init, off0],
                                bounds=(lo, hi), maxfev=3000)
            rms = float(np.sqrt(np.mean((ys - super_gaussian(xs, *popt)) ** 2)))
            if rms < best_rms:
                best_rms, best = rms, popt
        except Exception:
            pass
    return best

def sim_scan_pitch(start, stop, nsteps, true_center, sigma=0.015, amp=1000.0, noise=20.0):
    xs = np.linspace(start, stop, nsteps)
    ys = gaussian(xs, true_center, sigma, amp, 10.0) + np.random.normal(0, noise, nsteps)
    return xs, np.maximum(ys, 0.0)

def sim_zero_line(x, true_zero, slope=10.0, noise=0.003):
    """Point-wise simulated signal that crosses zero at `true_zero`."""
    return -(x - true_zero) * slope + random.gauss(0, noise)


def find_peak_centroid(xs, ys):
    ys_c = np.clip(ys - np.min(ys), 0, None)
    if ys_c.sum() == 0:
        return float(xs[len(xs)//2])
    return float(np.average(xs, weights=ys_c))

def find_zero_crossing(xs, ys):
    """Find where ys crosses zero by linear interpolation."""
    for i in range(len(ys) - 1):
        if ys[i] * ys[i+1] <= 0:
            x0, x1 = xs[i], xs[i+1]
            y0, y1 = ys[i], ys[i+1]
            if (y1 - y0) != 0:
                return float(x0 - y0 * (x1 - x0) / (y1 - y0))
    return float(xs[np.argmin(np.abs(ys))])

def fwhm_half_max(xs, ys):
    """FWHM via linear interpolation at half-maximum. Suitable for square/flat-top profiles."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    baseline = np.min(ys)
    peak     = np.max(ys)
    if peak <= baseline:
        return None
    half = baseline + (peak - baseline) / 2.0
    left = right = None
    for i in range(len(ys) - 1):
        if ys[i] <= half <= ys[i + 1] or ys[i] >= half >= ys[i + 1]:
            t = (half - ys[i]) / (ys[i + 1] - ys[i])
            x = xs[i] + t * (xs[i + 1] - xs[i])
            if left is None:
                left = x
            else:
                right = x
    if left is None or right is None:
        return None
    return abs(right - left)

# ─── EPICS interface (real or simulated) ─────────────────────────────────────
# ─── PV probing helpers ──────────────────────────────────────────────────────
def probe_pv(pv_name, timeout=2.0, try_rbv=False):
    """Connection-only check for a single PV. Returns (ok, detail).

    Shared by the pre-flight check, the Check PV button in the fault dialog and
    the Setup tab connection test, so they all report the same vocabulary.
    """
    name = (pv_name or "").strip()
    if not name:
        return False, "empty PV name"
    if not EPICS_AVAILABLE:
        return False, "pyepics not installed"
    try:
        # libca contexts are per-thread and this is called from worker threads.
        epics.ca.use_initial_context()
    except Exception:
        pass
    try:
        pv = epics.PV(name, connection_timeout=timeout)
        if pv.wait_for_connection(timeout=timeout):
            pv.disconnect()
            return True, "ok"
        if try_rbv:
            rbv = epics.PV(name + ".RBV", connection_timeout=timeout)
            if rbv.wait_for_connection(timeout=timeout):
                rbv.disconnect()
                return True, "ok (via .RBV)"
        return False, "timeout"
    except Exception as exc:
        msg = str(exc)
        # libca on Windows surfaces an unreachable PV as an access violation
        if "access violation" in msg.lower():
            return False, "timeout"
        return False, "error: %s" % msg


def describe_pv(pv_name, timeout=2.0):
    """Verbose single-PV diagnostic for the fault dialog Check PV button.

    Returns a multi-line human-readable report. Never raises.
    """
    name = (pv_name or "").strip()
    lines = []
    if not name:
        return "No PV name configured for this read — check the Setup tab."
    if not EPICS_AVAILABLE:
        return "pyepics is not installed; cannot check %s." % name
    try:
        epics.ca.use_initial_context()
    except Exception:
        pass

    def _one(nm, label):
        try:
            pv = epics.PV(nm, connection_timeout=timeout)
            connected = pv.wait_for_connection(timeout=timeout)
        except Exception as exc:
            lines.append("%-11s %s  ->  ERROR: %s" % (label, nm, exc))
            return
        if not connected:
            lines.append("%-11s %s  ->  NOT CONNECTED (no response in %.1fs)" % (label, nm, timeout))
            try:
                pv.disconnect()
            except Exception:
                pass
            return
        try:
            val = pv.get(timeout=timeout)
            sval = pv.get(as_string=True, timeout=timeout)
            lines.append("%-11s %s  ->  CONNECTED" % (label, nm))
            lines.append("            value      = %r" % (val,))
            lines.append("            as string  = %r" % (sval,))
            lines.append("            type/count = %s / %s" % (pv.type, pv.count))
            lines.append("            severity   = %s   status = %s" % (pv.severity, pv.status))
            ts = pv.timestamp
            if ts:
                lines.append("            timestamp  = %s" % (
                    datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")))
            if getattr(pv, "host", None):
                lines.append("            IOC host   = %s" % pv.host)
            if val is None:
                lines.append("            NOTE: connected, but the read still returned None.")
        except Exception as exc:
            lines.append("            read failed: %s" % exc)
        finally:
            try:
                pv.disconnect()
            except Exception:
                pass

    _one(name, "PV")
    if "." not in name:
        _one(name + ".RBV", "readback")
    _one(name.split(".")[0] + ".DMOV", "motion")
    return "\n".join(lines)


# ─── EPICS interface (real or simulated) ─────────────────────────────────────
class EpicsInterface:
    """Thin transport over pyepics. Reports failure honestly; it sets no policy.

    Callers decide what a failure means — see AlignmentWorker._read_float and
    _write, which turn one into an operator-visible critical fault.
    """

    def __init__(self, simulate=True):
        self.simulate = simulate
        self._sim_vals = {}

    def get(self, pv, as_string=False, timeout=3.0):
        """Return the PV value, or None if it could not be read.

        None means "no answer" — disconnected, unknown PV, or CA error. It is
        never a legitimate reading, which is what makes the fault check sound.
        """
        if self.simulate:
            return self._sim_vals.get(pv, 0.0)
        if not EPICS_AVAILABLE or not pv:
            return None
        try:
            return epics.caget(pv, as_string=as_string,
                               timeout=timeout, connection_timeout=timeout)
        except Exception:
            return None

    def put(self, pv, value, wait=True, timeout=30.0):
        """Write a PV. Returns (ok, reason). Never raises.

        epics.caput returns 1 on success, a negative code on a low-level CA
        failure, and None when it cannot connect — it does not raise. The old
        implementation returned True unconditionally, so every failed write was
        invisible and the "moved" log lines could be lies.
        """
        if self.simulate:
            self._sim_vals[pv] = value
            return True, ""
        if not EPICS_AVAILABLE:
            return False, "pyepics not installed"
        if not pv:
            return False, "empty PV name"
        try:
            ret = epics.caput(pv, value, wait=wait, timeout=timeout)
        except TypeError:
            # PV is a string/char type — retry with a string value
            try:
                ret = epics.caput(pv, str(value), wait=wait, timeout=timeout)
            except Exception as exc:
                return False, "caput raised %s: %s" % (type(exc).__name__, exc)
        except Exception as exc:
            return False, "caput raised %s: %s" % (type(exc).__name__, exc)
        if ret is None:
            return False, "PV not connected"
        try:
            if int(ret) < 0:
                return False, "caput failed or timed out (code %s)" % ret
        except (TypeError, ValueError):
            pass
        return True, ""

# ─── Alignment worker thread ─────────────────────────────────────────────────
class PVFaultAbort(Exception):
    """Raised when the operator aborts at a PV fault prompt.

    Unwinds out of the nested scan closures straight to AlignmentWorker.run(),
    where it cannot be confused with an ordinary "the fit failed" return value.
    """

    def __init__(self, pv, context=""):
        super().__init__("PV fault on %s: %s" % (pv, context))
        self.pv = pv
        self.context = context


class AlignmentWorker(QObject):
    log_signal       = pyqtSignal(str, str)   # (message, level)
    step_status      = pyqtSignal(int, str)   # (step_num, status)
    scan_point       = pyqtSignal(str, float, float)  # (substep_key, x, y)
    scan_peak        = pyqtSignal(str, float)          # (substep_key, peak_x)
    bpm_update       = pyqtSignal(float, float, float) # x, y, intensity
    feedback_update  = pyqtSignal(bool, bool)          # h, v
    substep_status   = pyqtSignal(str, str)            # (key "step_sub", status)
    finished         = pyqtSignal(bool)                # success
    confirm_needed   = pyqtSignal(str)                 # substep key, waiting for operator
    scan_results_ready = pyqtSignal(dict)              # fit results keyed by label
    stripe_status      = pyqtSignal(str)               # "Si"/"Rh"/"Pt"/"changing"
    pv_fault           = pyqtSignal(str, str, str)     # (pv, context, reason)
    pv_fault_cleared   = pyqtSignal(str)               # action the operator took
    paused_changed     = pyqtSignal(bool)              # True while blocked on a fault
    preflight_report   = pyqtSignal(list)              # [(label, pv, status), ...]

    def __init__(self, pvs, scan_params, row, simulate=True, skip_mirror=True,
                 mirror_stages=None, confirm_mode=False, enabled=None):
        super().__init__()
        self.pvs = pvs
        self.params = scan_params
        # Coerce once here rather than discovering a string mid-sequence, where
        # it surfaced as a generic "Unexpected error" after motors had moved.
        self.row = coerce_energy_row(row)
        self.simulate = simulate
        self.skip_mirror = skip_mirror
        self.mirror_stages = mirror_stages or []
        self.confirm_mode = confirm_mode
        # Substep keys the operator left enabled. None means "run everything".
        self.enabled       = None if enabled is None else set(enabled)
        self._mirror_in    = False   # tracked for real, not inferred from skip_mirror
        self._abort        = False
        self._scan_results = {}   # populated during scans; emitted via scan_results_ready
        self._confirm_event = threading.Event()
        # PV fault handshake. Separate Event from _confirm_event so a fault
        # raised during a confirm-wait cannot be answered by the Proceed button.
        self._fault_event     = threading.Event()
        self._fault_choice    = None    # "retry" | "abort"
        self._paused          = False
        self._pause_requested = False   # set by external_fault() on the UI thread
        self._pending_fault   = None    # (pv, context, reason) from a CA monitor
        self._faulted_pvs     = set()   # distinct PVs that have faulted this run
        self._ca_hint_shown   = False
        self._is_motor        = {}      # pv name -> bool, filled by pre-flight
        self._rtype           = {}      # pv name -> .RTYP string or None, filled by pre-flight
        self._deadbands       = {}      # motor pv -> arrival tolerance
        self.epics = EpicsInterface(simulate=simulate)

    def abort(self):
        self._abort = True

    def _check_abort(self):
        return self._abort

    def _sleep(self, secs):
        steps = max(1, int(secs / 0.05))
        for _ in range(steps):
            self._pause_point()
            if self._abort:
                return False
            time.sleep(0.05)
        return True

    def log(self, msg, level="info"):
        self.log_signal.emit(msg, level)

    def _motor_deadband(self, motor_pv):
        """Arrival tolerance for a motor, taken from its own .RDBD / .MRES."""
        if motor_pv in self._deadbands:
            return self._deadbands[motor_pv]
        vals = []
        for field in (".RDBD", ".MRES"):
            v = self.epics.get(motor_pv + field)
            try:
                vals.append(abs(float(v)))
            except (TypeError, ValueError):
                pass
        db = max(vals) if vals else 0.0
        self._deadbands[motor_pv] = max(db, 1e-9)
        return self._deadbands[motor_pv]

    def _motor_problem(self, motor_pv):
        """Decode .MSTA / .LVIO into a human reason, or None if healthy.

        Read with the raw accessor: not every record carries these fields, and
        a missing one must not itself be a fault.
        """
        try:
            msta = int(self.epics.get(motor_pv + ".MSTA") or 0)
        except (TypeError, ValueError):
            msta = 0
        flags = []
        if msta & 0x200:          # PROBLEM
            flags.append("the driver reports a problem (MSTA PROBLEM bit)")
        if msta & 0x1000:         # COMM_ERR
            flags.append("controller communication error (MSTA COMM_ERR bit)")
        if self.epics.get(motor_pv + ".LVIO"):
            flags.append("limit violation (.LVIO)")
        return "; ".join(flags) if flags else None

    def _verify_arrival(self, motor_pv, context, deadband):
        """Confirm the motor actually reached its target. Blocks on a fault."""
        while True:
            diff = self._read_float(motor_pv + ".DIFF", context)
            miss = self.epics.get(motor_pv + ".MISS")
            if not miss and abs(diff) <= deadband:
                return True
            rbv = self._read_float(motor_pv + ".RBV", context)
            val = self._read_float(motor_pv + ".VAL", context)
            reason = ("stopped at %.6g but the target is %.6g (.DIFF = %.6g, "
                      "deadband %.6g%s)" % (rbv, val, diff, deadband,
                                            ", .MISS is set" if miss else ""))
            if self._fault(motor_pv, context, reason) == "abort":
                raise PVFaultAbort(motor_pv, context)

    def _wait_motor_done(self, motor_pv):
        """Block until the motor finishes moving. Returns False only on abort.

        There is deliberately NO cap on how long the motion may take: CRL Y
        needs ~60 s for its in/out travel and a large Mono E change takes
        several minutes. Timeouts apply only to *absence of confirmation* — a
        field that will not read, a setpoint the motor never acts on, or an
        encoder that stops advancing while the record still claims to be
        moving. All of those go through the normal fault dialog.
        """
        name = (motor_pv or "").strip()
        if self.simulate or not name:
            return self._sleep(0.05)
        if not self._pv_is_motor(name):
            return True   # plain record: the put-callback was the confirmation
        context = f"waiting for {name} to finish moving"
        grace    = float(self.params.get("motor_start_grace", 5.0))
        stall    = float(self.params.get("motor_stall_timeout", 30.0))
        deadband = self._motor_deadband(name)
        t0 = last_progress = time.time()
        last_rbv = self._read_float(name + ".RBV", context)
        started  = False
        ticks    = 0
        while True:
            self._pause_point()
            if self._abort:
                return False
            dmov = self._read_float(name + ".DMOV", context)
            rbv  = self._read_float(name + ".RBV", context)
            if abs(rbv - last_rbv) > deadband:
                last_rbv, last_progress = rbv, time.time()

            # MSTA/LVIO change slowly; no need to read them at the poll rate.
            ticks += 1
            problem = self._motor_problem(name) if ticks % 10 == 1 else None
            if problem:
                if self._fault(name, context, problem) == "abort":
                    raise PVFaultAbort(name, context)
                t0 = last_progress = time.time()   # operator retried; restart the clocks
                started = False
                continue

            if not dmov:
                started = True
                if time.time() - last_progress > stall:
                    if self._fault(name, context,
                                   "reports moving but .RBV has not advanced for "
                                   "%gs (stuck at %.6g)" % (stall, rbv)) == "abort":
                        raise PVFaultAbort(name, context)
                    t0 = last_progress = time.time()
                    started = False
            else:
                diff = self._read_float(name + ".DIFF", context)
                if started or abs(diff) <= deadband:
                    return self._verify_arrival(name, context, deadband)
                if time.time() - t0 > grace:
                    if self._fault(name, context,
                                   "setpoint accepted but the motor has not started "
                                   "within %gs (.DIFF = %.6g)" % (grace, diff)) == "abort":
                        raise PVFaultAbort(name, context)
                    t0 = last_progress = time.time()
            time.sleep(0.05)

    def _wait_undulator(self):
        """Block until the undulator reports it has reached the new energy.

        Same shape as _wait_motor_done: no cap on travel time, only on absence
        of progress. A multi-keV gap change takes tens of seconds, and the
        Step 3 pitch scan is meaningless until the undulator lands.
        """
        busy_pv = (self.pvs.get("und_busy") or "").strip()
        if self.simulate or not busy_pv:
            return True
        rbv_pv  = (self.pvs.get("und_energy_rbv") or "").strip()
        context = "waiting for the undulator to reach its new energy"
        grace   = float(self.params.get("motor_start_grace", 5.0))
        stall   = float(self.params.get("motor_stall_timeout", 30.0))
        t0 = last_progress = time.time()
        last_e  = self._read_float(rbv_pv, context) if rbv_pv else None
        started = False
        self.log("  Waiting for the undulator…")
        while True:
            self._pause_point()
            if self._abort:
                return False
            busy = self._read_float(busy_pv, context)
            if rbv_pv:
                energy = self._read_float(rbv_pv, context)
                if last_e is None or abs(energy - last_e) > 1e-4:
                    last_e, last_progress = energy, time.time()
            if busy:
                started = True
                if time.time() - last_progress > stall:
                    if self._fault(busy_pv, context,
                                   "the undulator reports busy but its energy readback "
                                   "has not changed for %gs" % stall) == "abort":
                        raise PVFaultAbort(busy_pv, context)
                    t0 = last_progress = time.time()
                    started = False
            elif started or time.time() - t0 > grace:
                self.log("  Undulator in position.", "ok")
                return True
            time.sleep(0.05)

    def _wait_all_motors(self, pv_names, label="stages"):
        """Await several stages that were all commanded before waiting.

        Setpoints are issued first and awaited afterwards so the stages travel
        concurrently rather than one after another.
        """
        pending = [p for p in dict.fromkeys(pv_names) if p and self._pv_is_motor(p)]
        if not pending:
            return True
        self.log("  Waiting for %d %s to finish moving…" % (len(pending), label))
        for pv in pending:
            if not self._wait_motor_done(pv):
                return False
        return True

    def confirm(self):
        """Called from the UI thread when operator clicks Proceed."""
        self._confirm_event.set()

    def request_confirm(self, substep_key):
        """Pause worker until operator clicks Proceed (or abort). Returns False if aborted."""
        if not self.confirm_mode:
            return True
        self._confirm_event.clear()
        self.confirm_needed.emit(substep_key)
        while not self._confirm_event.is_set():
            self._pause_point()
            if self._abort:
                return False
            time.sleep(0.05)
        return True

    # ── PV fault handling ──────────────────────────────────────────

    def fault_retry(self):
        """UI thread: operator clicked Try Again."""
        self._fault_choice = "retry"
        self._fault_event.set()

    def fault_abort(self):
        """UI thread: operator clicked Abort at a fault prompt."""
        self._fault_choice = "abort"
        self._abort = True
        self._fault_event.set()

    def external_fault(self, pv_name, context, reason):
        """UI thread: a live CA monitor saw a PV drop. Pause at the next safe point.

        Deliberately does not emit pv_fault itself. The worker emits when it
        reaches _pause_point(), so the UI never shows PAUSED while a motor is
        still moving.
        """
        self._pending_fault   = (pv_name, context, reason)
        self._pause_requested = True

    def _fault(self, pv_name, context, reason):
        """Log a critical error, pause the sequence, and block until the
        operator answers. Returns "retry" or "abort".

        Simulation never faults — this is the backstop behind the transport
        layer, so a future code path that slips past it degrades to the old
        behaviour instead of hanging a simulated run on a dialog.
        """
        if self.simulate:
            self.log(f"  (sim) ignoring would-be PV fault on '{pv_name}': {reason}", "warn")
            return "retry"
        shown = pv_name or "(no PV name configured)"
        self.log(f"CRITICAL — PV FAULT: {shown}", "error")
        self.log(f"  Problem: {reason}", "error")
        self.log(f"  While:   {context}", "error")
        self.log("  Sequence PAUSED. Nothing further will move until you respond.", "error")
        self._faulted_pvs.add(shown)
        if len(self._faulted_pvs) >= 3 and not self._ca_hint_shown:
            self._ca_hint_shown = True
            self.log("  NOTE: three or more different PVs have faulted in this run "
                     "even though pre-flight passed. That pattern usually means the "
                     "worker thread lost its EPICS channel-access context rather "
                     "than the IOCs being down.", "warn")
        self._paused       = True
        self._fault_choice = None
        self._fault_event.clear()
        self.paused_changed.emit(True)
        self.pv_fault.emit(shown, context, reason)
        # Poll rather than Event.wait() so the main Abort button still works.
        while not self._fault_event.is_set():
            if self._abort:
                self._fault_choice = "abort"
                break
            time.sleep(0.05)
        self._paused = False
        choice = self._fault_choice or "abort"
        self.paused_changed.emit(False)
        self.pv_fault_cleared.emit(choice)
        if choice == "retry":
            self.log(f"  Operator chose Try Again — retrying {shown}.", "warn")
        return choice

    def _fault_many(self, entries, context):
        """Report several faulted PVs at once and block on the last.

        The earlier emits populate the dialog's list (and its Check PV combo)
        without blocking, so the operator sees every failure in one place.
        """
        if self.simulate or not entries:
            return "retry"
        for pv, reason in entries[:-1]:
            self.pv_fault.emit(pv or "(no PV name configured)", context, reason)
        pv, reason = entries[-1]
        return self._fault(pv, context, reason)

    def _pause_point(self):
        """Enter the fault handshake if a CA monitor flagged a disconnect.

        Called from every polling loop so an asynchronous fault stops the
        sequence between operations rather than mid-move.
        """
        if not self._pause_requested:
            return
        self._pause_requested = False
        pv_name, context, reason = self._pending_fault or (
            "(unknown)", "live PV monitor", "connection lost")
        self._pending_fault = None
        if self._fault(pv_name, context, reason) == "abort":
            raise PVFaultAbort(pv_name, context)

    def _read(self, pv_name, context, as_string=False):
        """Checked PV read. Returns the value — guaranteed never None.

        Raises PVFaultAbort if the operator aborts.
        """
        while True:
            self._pause_point()
            if self._abort:
                raise PVFaultAbort(pv_name, context)
            name = (pv_name or "").strip()
            if not name:
                if self.simulate:
                    return 0.0   # no real PVs in simulation; blank means unused
                reason = "no PV name is configured for this read"
            else:
                value = self.epics.get(name, as_string=as_string)
                if value is not None:
                    return value
                if self.simulate:
                    return 0.0
                reason = "read returned None (PV is disconnected)"
            if self._fault(name, context, reason) == "abort":
                raise PVFaultAbort(name, context)

    def _read_float(self, pv_name, context, allow_blank=False, default=None):
        """Checked numeric read. Returns a float, never None.

        allow_blank distinguishes "not configured" (a deliberate choice, returns
        `default`) from "configured but disconnected" (a fault).
        """
        name = (pv_name or "").strip()
        if not name and allow_blank:
            return default
        while True:
            value = self._read(name, context)
            try:
                return float(value)
            except (TypeError, ValueError):
                if self.simulate:
                    return 0.0
                if self._fault(name, context,
                               f"read returned a non-numeric value ({value!r})") == "abort":
                    raise PVFaultAbort(name, context)

    def _pv_is_motor(self, pv_name):
        """True if this PV is an EPICS motor record, i.e. its .DMOV connects.

        Classified in bulk during pre-flight; probed lazily if that was skipped.
        """
        name = (pv_name or "").strip()
        if not name or self.simulate or not EPICS_AVAILABLE:
            return False
        if name not in self._is_motor:
            ok, _detail = probe_pv(name + ".DMOV", timeout=1.0)
            self._is_motor[name] = ok
        return self._is_motor[name]

    def _write(self, pv_name, value, context, wait=None):
        """Checked PV write. Raises PVFaultAbort if the operator aborts.

        Motor records get the setpoint with NO put-callback wait: on a motor
        that callback does not fire until the move finishes, so a legitimately
        slow stage (CRL Y takes ~60 s, a large Mono E move several minutes)
        would look like a failed write. Their completion is tracked separately
        by _wait_motor_done(). For a plain record the put-callback *is* the
        write confirmation, and it should arrive promptly.
        """
        while True:
            self._pause_point()
            if self._abort:
                raise PVFaultAbort(pv_name, context)
            name = (pv_name or "").strip()
            if not name:
                if self.simulate:
                    return   # no-op, matching the pre-existing simulation behaviour
                reason = "no PV name is configured for this write"
            else:
                use_wait = (not self._pv_is_motor(name)) if wait is None else wait
                ok, reason = self.epics.put(
                    name, value, wait=use_wait,
                    timeout=float(self.params.get("pv_ack_timeout", 10.0)))
                if ok:
                    return
                if self.simulate:
                    return
            if self._fault(name, context, f"write of {value!r} failed: {reason}") == "abort":
                raise PVFaultAbort(name, context)

    def _step_on(self, substep_key):
        """True if this substep is enabled for this run."""
        return self.enabled is None or substep_key in self.enabled

    def _skip(self, substep_key):
        """Report and swallow a substep the operator switched off."""
        if self._step_on(substep_key):
            return False
        self.substep_status.emit(substep_key, "skipped")
        label = substep_key.split("_", 1)[-1].upper()
        self.log(f"  {label} skipped \u2014 disabled for this run.", "warn")
        return True

    def _begin_chapter(self, number, title):
        """Announce a chapter and clear the AutoFeedback override.

        15IDA:userTran6.O forces both feedback loops on while it is 1, which
        silently defeats every write to feedback_h/feedback_v. Clearing it at
        the start of each chapter is idempotent and cheap.
        """
        self.step_status.emit(number, "running")
        self.log(f"\u2501\u2501 Step {number} \u2014 {title} \u2501\u2501")
        auto_pv = (self.pvs.get("auto_feedback") or "").strip()
        if auto_pv:
            self._write(auto_pv, 0, f"{number} \u2014 clear the AutoFeedback override")

    def _pre_scan(self, substep_key, allow_h=False):
        """Everything that must hold before a scan starts."""
        self._require_beam(substep_key)
        self._require_feedback_off(substep_key, allow_h=allow_h)

    def _require_beam(self, substep_key):
        """Refuse to scan while the upstream shutter is blocking the beam.

        Unlike the feedback loops, this is not something the app may fix: the
        PSS owns the shutter. So it pauses and waits for the operator to open
        it and press Try Again.
        """
        if self.simulate:
            return
        pv = (self.pvs.get("shutter_blocking") or "").strip()
        if not pv:
            return
        context = f"{substep_key} \u2014 upstream shutter check"
        while True:
            blocking = self._read_float(pv, context)
            if not blocking:
                return
            state = self.epics.get(pv, as_string=True) or blocking
            if self._fault(pv, context,
                           f"the upstream shutter is blocking the beam (reads "
                           f"{state!r}). Open it, then press Try Again.") == "abort":
                raise PVFaultAbort(pv, context)

    def _require_feedback_off(self, substep_key, allow_h=False):
        """Assert the feedback loops are off before a scan, correcting if not.

        The sequence used to command these PVs without ever reading them back.
        H is legitimately on from 5A onward, so `allow_h` exempts it there; V
        must be off for every scan.
        """
        if self.simulate:
            return
        checks = [("V", "feedback_v")]
        if not allow_h:
            checks.insert(0, ("H", "feedback_h"))
        for axis, key in checks:
            pv = (self.pvs.get(key) or "").strip()
            if not pv:
                continue
            state = self._read_float(pv, f"{substep_key} \u2014 check {axis} feedback state",
                                     allow_blank=True)
            if state:
                self.log(f"  WARNING: {axis} feedback was ON before {substep_key} \u2014 "
                         f"turning it off.", "warn")
                self._write(pv, 0, f"{substep_key} \u2014 force {axis} feedback off")

    def _signal_pv(self, choice_key, default_choice="BPM Intensity"):
        """Resolve a configured signal-source choice to an actual PV name.

        `pvs.get("ion_chamber", fallback)` is wrong here: the key normally
        exists with an empty value, so the fallback would never apply.
        """
        choice = self.params.get(choice_key, default_choice)
        if choice == "Ion Chamber":
            return self.pvs.get("ion_chamber") or self.pvs.get("bpm_intensity", "")
        return self.pvs.get("bpm_intensity") or self.pvs.get("ion_chamber", "")

    def _smart_scan_peak(self, motor_pv, center, half_range, steps,
                         sim_fn, substep_key):
        """Adaptive scan for intensity peak. Returns (peak_pos, sigma) or (None, None)."""
        p = self.params
        edge_frac  = p.get("smart_edge_fraction",    0.2)
        max_ext    = p.get("smart_max_extend_steps", 10)
        fine_range = p.get("smart_fine_sigma_range", 2.0)
        fine_iter  = p.get("smart_fine_scan_iter",   3)
        signal_pv  = self._signal_pv("dcm_signal")

        lo, hi = center - half_range, center + half_range
        step_size = (hi - lo) / max(steps - 1, 1)
        ext_n = max(steps // 4, 1)

        xs_all, ys_all = [], []

        def sorted_all():
            """xs_all/ys_all in ascending x.

            Extension scans append out of order, so anything that inspects
            neighbouring points or the range ends must sort first.
            """
            if not xs_all:
                return [], []
            pairs = sorted(zip(xs_all, ys_all))
            return [q[0] for q in pairs], [q[1] for q in pairs]

        def do_scan(a, b, n):
            if self.simulate:
                scan_xs, scan_ys = sim_fn(a, b, n)
                for x, y in zip(scan_xs, scan_ys):
                    self.scan_point.emit(substep_key, float(x), float(y))
                    xs_all.append(float(x)); ys_all.append(float(y))
            else:
                scan_xs = list(np.linspace(a, b, n))
                scan_ys = []
                for x in scan_xs:
                    if self._abort: return None, None
                    self._write(motor_pv, x, f"{substep_key} — move {motor_pv} to {x:.6g}")
                    if not self._wait_motor_done(motor_pv): return None, None
                    if not self._sleep(p["settle_time"]): return None, None
                    y = self._read_float(signal_pv, f"{substep_key} — read scan signal")
                    scan_ys.append(y)
                    self.scan_point.emit(substep_key, float(x), float(y))
                    xs_all.append(float(x)); ys_all.append(float(y))
                    if self._abort: return None, None
            return scan_xs, scan_ys

        # initial scan
        rx, ry = do_scan(lo, hi, steps)
        if rx is None: return None, None

        # extend if the peak sits near a boundary
        for _ in range(max_ext):
            xs_s, ys_s = sorted_all()
            popt = fit_super_gaussian(xs_s, ys_s)
            if popt is None: break
            pk = popt[1]
            x_lo, x_hi = xs_s[0], xs_s[-1]
            span = x_hi - x_lo
            if pk < x_lo + edge_frac * span:
                rx, ry = do_scan(x_lo - step_size * ext_n, x_lo - step_size, ext_n + 1)
                if rx is None: return None, None
            elif pk > x_hi - edge_frac * span:
                rx, ry = do_scan(x_hi + step_size, x_hi + step_size * ext_n, ext_n + 1)
                if rx is None: return None, None
            else:
                break

        xs_s, ys_s = sorted_all()
        popt = fit_super_gaussian(xs_s, ys_s)
        if popt is None:
            self.log(f"  WARNING: could not fit peak in {substep_key} scan — using best estimate", "warn")
            best_x = float(np.asarray(xs_s)[np.argmax(ys_s)])
            return best_x, None

        pk, sig, p_exp = popt[1], popt[2], popt[3]

        # fine scan iterations
        prev_sig = sig
        for _ in range(fine_iter):
            fr = fine_range * abs(prev_sig)
            rx, ry = do_scan(pk - fr, pk + fr, steps)
            if rx is None: return None, None
            xs_s, ys_s = sorted_all()
            popt2 = fit_super_gaussian(xs_s, ys_s)
            if popt2 is None: break
            pk, sig, p_exp = popt2[1], popt2[2], popt2[3]
            if prev_sig / (sig + 1e-30) < 2.0: break
            prev_sig = sig

        # FWHM from the same fit that produced `sig` — this previously mixed the
        # exponent from the coarse fit with sigma from the refined one.
        fwhm = 2.0 * sig * (np.log(2) ** (1.0 / max(p_exp, 0.5)))
        best_x = float(np.asarray(xs_s)[np.argmax(ys_s)])
        final = pk if abs(pk - best_x) < fwhm else best_x
        self.scan_peak.emit(substep_key, final)
        return final, sig

    def _scan_to_zero(self, motor_pv, signal_pv, start, step, substep_key,
                      max_span, sim_fn=None, signal_label="signal"):
        """Step from `start` until two points lie past the zero crossing.

        The configured start position and step size are kept, but there is no
        pre-decided end: the scan walks in the configured direction watching the
        sign of the signal and stops as soon as two points sit beyond the
        crossing. Previously all three BPM-zero scans swept a fixed window and,
        if the crossing fell outside it, find_zero_crossing quietly returned the
        closest-to-zero point and the motor was driven there anyway.

        A scan that never crosses is bounded by `max_span` and then faults, so a
        wrong sign or a dead detector cannot walk the motor toward a limit.

        Returns the interpolated crossing position, or None if aborted.
        """
        if not step:
            step = 1e-9
        is_motor = self._pv_is_motor(motor_pv)
        settle   = (self.params.get("settle_time", 0.1) if is_motor
                    else self.params.get("piezo_settle_time", 0.2))
        context  = f"{substep_key} \u2014 scanning {motor_pv} for {signal_label} = 0"
        budget   = abs(max_span)
        xs, ys, past = [], [], 0
        x = float(start)

        while True:
            if self._abort:
                return None
            self._write(motor_pv, x, f"{substep_key} \u2014 step {motor_pv} to {x:.6g}")
            if is_motor and not self._wait_motor_done(motor_pv):
                return None
            if not self._sleep(settle):
                return None
            if self.simulate and sim_fn is not None:
                y = float(sim_fn(x))
            else:
                y = self._read_float(signal_pv,
                                     f"{substep_key} \u2014 read {signal_label}")
            xs.append(float(x))
            ys.append(float(y))
            self.scan_point.emit(substep_key, float(x), float(y))

            if past:
                past += 1
            elif len(ys) >= 2 and ys[-2] * ys[-1] <= 0:
                past = 1              # this point is the first beyond the crossing
            if past >= 2:
                break                 # two points past: nothing more to learn

            if abs(x - float(start)) > budget:
                if self._fault(motor_pv, context,
                               "no %s zero crossing within %.6g of the start position "
                               "%.6g (last value %.6g)"
                               % (signal_label, budget, float(start), y)) == "abort":
                    raise PVFaultAbort(motor_pv, context)
                budget += abs(max_span)   # operator retried: allow another span
                self.log("  Extending the search to %.6g." % budget, "warn")
            x += step

        zero = find_zero_crossing(xs, ys)
        self.scan_peak.emit(substep_key, zero)
        return zero

    def _mirror_yz_pvs(self):
        """Resolve VDM:Y / VFM:Y from the mirror-stage table.

        Falls back to the same hard-coded names Step 4 uses when no stage name
        matches, so pre-flight checks exactly what the sequence will drive.
        """
        vdm_pv = "ID15A1:DMS:VDM:Y"
        vfm_pv = "ID15A1:DMS:VFM:Y"
        for stage in self.mirror_stages:
            nm = stage.get("name", "")
            if "VDM" in nm:
                vdm_pv = stage["pv"]
            elif "VFM" in nm:
                vfm_pv = stage["pv"]
        return vdm_pv, vfm_pv

    def _jjc_size_pv(self):
        """PV of the "JJC Size" mirror stage, or "" if it is not configured."""
        for stage in self.mirror_stages:
            name = stage.get("name", "")
            if "JJC" in name and "Size" in name:
                return (stage.get("pv") or "").strip()
        return ""

    def _required_pvs(self):
        """PVs this run will touch, as [(label, pv_name, try_rbv, writable)].

        Computed dynamically — skip_mirror, blank names and the configured
        signal sources all change the set. `writable` marks the ones the
        sequence drives, which are the ones worth classifying as motors.
        """
        pvs, p = self.pvs, self.params
        out = []

        def add(label, name, try_rbv=False, writable=False):
            out.append((label, (name or "").strip(), try_rbv, writable))

        # ── always used ──
        add("H feedback",       pvs.get("feedback_h"), writable=True)
        add("V feedback",       pvs.get("feedback_v"), writable=True)
        if (pvs.get("auto_feedback") or "").strip():
            add("Auto Feedback On", pvs["auto_feedback"], writable=True)
        # Read-only, but every scan depends on it, so a missing or dead shutter
        # PV should stop the run before anything moves.
        if any(k in _SCAN_ROUTES for k in (self.enabled or _SCAN_ROUTES)):
            add("Upstream shutter", pvs.get("shutter_blocking"))
        for key, label in (("bpm_sen", "BPM sensitivity"),
                           ("ic_sen_unit", "IC sensitivity unit"),
                           ("ic_sen_num", "IC sensitivity num")):
            if (pvs.get(key) or "").strip():
                add(label, pvs[key], writable=True)
        add("Undulator energy", pvs.get("und_energy"), writable=True)
        if (pvs.get("und_harmonic") or "").strip():
            add("Undulator harmonic", pvs["und_harmonic"], writable=True)
        if (pvs.get("und_start") or "").strip():
            add("Undulator start", pvs["und_start"], writable=True)
        if (pvs.get("und_busy") or "").strip():
            add("Undulator busy flag", pvs["und_busy"])
        if (pvs.get("und_energy_rbv") or "").strip():
            add("Undulator energy RBV", pvs["und_energy_rbv"])
        add("Mono energy",     pvs.get("mono_energy"), try_rbv=True, writable=True)
        add("DCM roll",        pvs.get("roll"),  try_rbv=True, writable=True)
        add("DCM pitch",       pvs.get("pitch"), try_rbv=True, writable=True)
        add("DCM piezo pitch", pvs.get("piezo_pitch"), writable=True)  # 3A and 5B
        add("DCM piezo roll",  pvs.get("piezo_roll"), writable=True)
        add("DCM scan signal", self._signal_pv("dcm_signal"))
        add("BPM X",           pvs.get("bpm_x"))
        add("BPM Y",           pvs.get("bpm_y"))
        add("BPM intensity",   pvs.get("bpm_intensity"))
        # 5C drives the mirror pitch piezo whether or not Step 4 ran, and 4B2
        # centres it. Added even when blank: a step that is switched on but has
        # no PV should stop pre-flight, not vanish with a log line.
        if self._step_on("5_5c") or (not self.skip_mirror and self._step_on("4_4B2")):
            add("Mirror piezo pitch", pvs.get("mir_piezo_pitch"), writable=True)

        # Mirror stages are driven at 2C unconditionally, and again at 4B or 5.
        for stage in self.mirror_stages:
            nm = (stage.get("pv") or "").strip()
            if nm:
                add("Stage: %s" % stage.get("name", nm), nm, writable=True)

        # _apply_mirror_stripe runs in both branches. These are module constants
        # rather than entries in `pvs`, which makes them easy to overlook.
        add("Mirror stripe VFM:X", _STRIPE_VFM_X_PV, try_rbv=True, writable=True)
        add("Mirror stripe VDM:X", _STRIPE_VDM_X_PV, try_rbv=True, writable=True)

        # ── only when Step 4 runs ──
        if not self.skip_mirror:
            top = (pvs.get("mir_slit_top") or "").strip()
            bot = (pvs.get("mir_slit_bot") or "").strip()
            if top and bot:      # mirrors the `if top_pv and bot_pv` guard in 4A
                add("Mirror slit top",    top, try_rbv=True, writable=True)
                add("Mirror slit bottom", bot, try_rbv=True, writable=True)
            if self._step_on("4_4C"):
                add("Mirror pitch motor", pvs.get("mir_pitch_motor"),
                    try_rbv=True, writable=True)
            vdm_pv, vfm_pv = self._mirror_yz_pvs()
            add("VDM:Y", vdm_pv, try_rbv=True, writable=True)
            add("VFM:Y", vfm_pv, try_rbv=True, writable=True)
            if p.get("mir_signal", "BPM Intensity") != "BPM Intensity":
                add("Mirror scan signal", self._signal_pv("mir_signal"))

        seen, uniq = set(), []
        for label, name, try_rbv, writable in out:
            if name and name in seen:
                continue
            if name:
                seen.add(name)
            uniq.append((label, name, try_rbv, writable))
        return uniq

    def _writable_pvs(self):
        """The subset of _required_pvs() that the sequence actually drives."""
        return [(label, name) for label, name, _rbv, writable in self._required_pvs()
                if writable and name]

    def active_pv_set(self):
        """PV names this run touches, base names included.

        Used to filter live-monitor disconnects: editing an unused PV name, or a
        dead sensitivity readback, must not pause a run that never reads it.
        """
        names = set()
        for _label, name, _rbv, _w in self._required_pvs():
            if name:
                names.add(name)
                names.add(name.split(".")[0])
        return names

    def _preflight(self):
        """Connect-test everything the run needs, before anything moves.

        Runs in the worker thread under its own CA context, so it also proves
        the context attach worked. The connection pass below runs concurrently
        in a ThreadPoolExecutor: probe_pv() calls epics.ca.use_initial_context()
        itself on every call, so each pool thread attaches its own CA context
        exactly as this thread did -- SetupTab._test_epics() already runs the
        same probe_pv() concurrently for the same reason. (This used to be
        serial on the theory that ad-hoc threads would each need their own
        attach; that theory does not hold for probe_pv, which handles it
        itself, so it no longer justifies paying for every PV one at a time.)

        Motor/plain classification reads .RTYP -- a dbCommon field every
        record type answers immediately, connected or not deliberately timed
        out -- instead of burning a full timeout on a .DMOV probe for every
        plain record. A PV whose .RTYP does not answer (or answers with
        something unreadable) falls back to the old .DMOV probe for that PV
        alone, since some sites front a motor with a non-'motor' record.
        """
        self.log("━━ Pre-flight — checking PV connections ━━")
        if self.simulate:
            self.log("  Simulation mode — pre-flight skipped.", "warn")
            self.preflight_report.emit([("(simulation)", "", "skipped")])
            return
        while True:
            required = self._required_pvs()
            rows, bad = [], []

            # ── Connection pass: concurrent, one probe per unique PV name ──
            # _required_pvs() already dedupes by name, but a blank name is
            # never probed and several labels could in principle still share
            # one name, so dedupe again here defensively before submitting.
            to_probe = {}   # name -> try_rbv (first row for that name wins)
            for _label, name, try_rbv, _w in required:
                if name and name not in to_probe:
                    to_probe[name] = try_rbv
            probe_results = {}   # name -> (ok, detail)
            if to_probe:
                # Not a `with` block: Executor.__exit__ calls shutdown(wait=True),
                # which would block on every still-in-flight probe_pv() call
                # before an abort raised inside the loop could actually
                # propagate -- silently reintroducing the serial stall this
                # rewrite exists to remove. shutdown() is called by hand in
                # `finally` instead, with wait=False, so an abort returns
                # immediately and any still-running probes are left to finish
                # in the background (harmless: probe_pv only reads).
                ex = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(len(to_probe), 1))
                try:
                    futures = {ex.submit(probe_pv, name, 1.0, try_rbv): name
                               for name, try_rbv in to_probe.items()}
                    pending = set(futures)
                    while pending:
                        done, pending = concurrent.futures.wait(pending, timeout=0.05)
                        for f in done:
                            probe_results[futures[f]] = f.result()
                        # Abort responsiveness lives in the completion wait,
                        # not per-PV, now that the probes run concurrently.
                        self._pause_point()
                        if self._abort:
                            raise PVFaultAbort("(pre-flight)",
                                               "aborted during pre-flight")
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

            for label, name, _try_rbv, _w in required:
                if not name:
                    rows.append((label, "", "not configured"))
                    bad.append(("", "%s has no PV name configured" % label))
                    continue
                ok, detail = probe_results.get(name, (False, "not probed"))
                rows.append((label, name, detail))
                if not ok:
                    bad.append((name, "did not connect (%s)" % detail))
            n_ok = sum(1 for _l, _p, st in rows if st.startswith("ok"))

            # ── Classification pass: .RTYP first, .DMOV fallback per PV ──
            self._is_motor = {}
            self._rtype = {}
            n_motors = 0
            for label, name in self._writable_pvs():
                self._pause_point()
                if self._abort:
                    raise PVFaultAbort("(pre-flight)", "aborted during pre-flight")
                rtyp_raw = self.epics.get(name + ".RTYP", as_string=True, timeout=1.0)
                rtyp = (rtyp_raw or "").strip()
                self._rtype[name] = rtyp or None
                if rtyp:
                    is_motor = (rtyp.lower() == "motor")
                    type_desc = "motor record" if is_motor else "%s record" % rtyp
                else:
                    is_motor, _d = probe_pv(name + ".DMOV", timeout=1.0)
                    type_desc = ("motor record" if is_motor
                                 else "plain record (RTYP unavailable)")
                self._is_motor[name] = is_motor
                n_motors += 1 if is_motor else 0
                rows.append((label + "  → type", name, type_desc))
            self.preflight_report.emit(rows)
            self.log("  %d of %d driven PVs are motor records."
                     % (n_motors, len(self._is_motor)))
            if not bad:
                self.log("  %d / %d PVs connected. Pre-flight passed."
                         % (n_ok, len(rows)), "ok")
                return
            self.log("  %d / %d PVs connected — %d FAILED. Nothing has been moved."
                     % (n_ok, len(rows), len(bad)), "error")
            for name, reason in bad:
                self.log("    %s %s" % (name or "(unnamed)", reason), "error")
            if self._fault_many(bad, "pre-flight PV connection check") == "abort":
                raise PVFaultAbort(bad[0][0] or "(unnamed)",
                                   "pre-flight PV connection check")

    def run(self):
        try:
            if not self.simulate and EPICS_AVAILABLE:
                # libca contexts are per-thread and pyepics resolves caget through
                # a module-level PV cache whose channels belong to the GUI thread.
                # Without this attach every read in this thread comes back None.
                try:
                    epics.ca.use_initial_context()
                except Exception as exc:
                    self.log(f"CRITICAL: cannot attach the EPICS CA context in the "
                             f"worker thread: {exc}", "error")
                    self.log("  Nothing has been moved. Stopping before any action.", "error")
                    self.finished.emit(False)
                    return
            self._run_sequence()
        except PVFaultAbort as e:
            self._abort_cleanup(f"stopped after a PV fault on {e.pv}")
        except Exception as e:
            import traceback
            self.log(f"Unexpected error: {e}", "error")
            for line in traceback.format_exc().rstrip().splitlines():
                self.log(f"    {line}", "error")
            self.finished.emit(False)

    def _run_sequence(self):
        pvs = self.pvs
        row = self.row
        p   = self.params

        # Verify every PV the run needs before anything moves — in particular
        # before 2A switches the BPM feedback off.
        self._preflight()

        # Emit initial BPM values so readouts are populated immediately
        self.bpm_update.emit(0.0, 0.0, 0.0)
        self.feedback_update.emit(False, False)

        # ── Step 1: Load settings ──────────────────────────────────────
        self._begin_chapter(1, "Load energy table settings")
        if not self._skip("1_1a"):
            self.substep_status.emit("1_1a", "running")
            if not self._sleep(0.3): return self._abort_cleanup()
            self.log(f"  Mono E   = {row['mono_e']} keV")
            self.log(f"  Und E    = {row['ue']} keV")
            self.log(f"  Roll SP  = {row['roll']}")
            self.log(f"  Pitch SP = {row['pitch']}")
            self.substep_status.emit("1_1a", "done")
        self.step_status.emit(1, "done")
        self.log("Step 1 complete.", "ok")

        # ── Step 2: Apply energy table settings ───────────────────────
        self._begin_chapter(2, "Apply energy table settings")

        # 2a: Turn off BPM feedback
        if not self._skip("2_2a"):
            self.substep_status.emit("2_2a", "running")
            self.log(f"  [{pvs['feedback_h']}] → 0  (H feedback OFF)", "warn")
            self._write(pvs['feedback_h'], 0, "2A — disable H feedback")
            self.log(f"  [{pvs['feedback_v']}] → 0  (V feedback OFF)", "warn")
            self._write(pvs['feedback_v'], 0, "2A — disable V feedback")
            self.feedback_update.emit(False, False)
            if not self._sleep(0.4): return self._abort_cleanup()
            self.substep_status.emit("2_2a", "done")

        # 2b: Apply energy table settings (undulator + DCM)
        if not self._skip("2_2b"):
            self.substep_status.emit("2_2b", "running")
            self.log("  Moving motors to energy table setpoints…")
            if pvs.get("und_harmonic"):
                self._write(pvs["und_harmonic"], row["harmonic"], "2B — set undulator harmonic")
                self.log(f"  [{pvs['und_harmonic']}] → {row['harmonic']}  (harmonic)", "ok")
                if not self._sleep(0.15): return self._abort_cleanup()
            self._write(pvs["und_energy"], row["ue"], "2B — set undulator energy")
            self.log(f"  [{pvs['und_energy']}] → {row['ue']} keV  (undulator energy)", "ok")
            if not self._sleep(0.15): return self._abort_cleanup()
            if pvs.get("und_start"):
                self._write(pvs["und_start"], 1, "2B — start undulator move")
                self.log(f"  [{pvs['und_start']}] → 1  (start undulator move)", "ok")
                if not self._sleep(0.15): return self._abort_cleanup()
            # The sequence used to go straight into the Step 3 pitch scan while the
            # undulator was still travelling.
            if not self._wait_undulator(): return self._abort_cleanup()
            for key, pv_key, unit in [
                ("mono_e", "mono_energy", "keV"),
                ("roll",   "roll",        ""),
                ("pitch",  "pitch",       ""),
            ]:
                self._write(pvs[pv_key], row[key], f"2B — set {pv_key} to {row[key]}")
                self.log(f"  [{pvs[pv_key]}] → {row[key]} {unit}", "ok")
                if not self._sleep(0.15): return self._abort_cleanup()

            # Detector gain follows the energy. These three columns have been in the
            # energy table all along but nothing ever wrote them to hardware.
            for key, pv_key, what in (
                ("bpm_sen",     "bpm_sen",     "BPM sensitivity"),
                ("ic_sen_unit", "ic_sen_unit", "ion chamber sensitivity unit"),
                ("ic_sen_num",  "ic_sen_num",  "ion chamber sensitivity num"),
            ):
                pv_name = (pvs.get(pv_key) or "").strip()
                value   = row.get(key, "")
                if not pv_name or value == "":
                    continue
                self._write(pv_name, value, f"2B — set {what}")
                self.log(f"  [{pv_name}] → {value}  ({what})", "ok")
                if not self._sleep(0.1): return self._abort_cleanup()
            self.substep_status.emit("2_2b", "done")

        # 2c: Mirror out
        if not self._skip("2_2c"):
            self.substep_status.emit("2_2c", "running")
            self.log("  Retracting mirror from beam path…")
            for stage in self.mirror_stages:
                if stage["pv"].strip():
                    self._write(stage["pv"], stage["val_out"],
                                f"2C — move {stage['name']} OUT")
                    self.log(f"  [{stage['pv']}] → {stage['val_out']}  ({stage['name']} OUT)", "ok")
                    if not self._sleep(0.1): return self._abort_cleanup()
            # Every setpoint is issued above so the stages travel concurrently; only
            # now wait for them. CRL Y alone takes ~60 s, so the old 0.4 s sleep
            # announced "Mirror retracted." while it was still moving.
            if not self._wait_all_motors([st["pv"] for st in self.mirror_stages],
                                         "mirror stages"):
                return self._abort_cleanup()
            self._mirror_in = False
            self.log("  Mirror retracted.", "ok")
            self.substep_status.emit("2_2c", "done")
        self.step_status.emit(2, "done")
        self.log("Step 2 complete.", "ok")

        # ── Step 3: DCM piezo alignment ────────────────────────────────
        self._begin_chapter(3, "DCM piezo alignment")

        # 3a: Center piezos
        if not self._skip("3_3a"):
            self.substep_status.emit("3_3a", "running")
            center = p["piezo_center"]
            self.log(f"  [{pvs['piezo_pitch']}] → {center}  (center)")
            self._write(pvs['piezo_pitch'], center, "3A — centre DCM pitch piezo")
            self.log(f"  [{pvs['piezo_roll']}] → {center}  (center)")
            self._write(pvs['piezo_roll'], center, "3A — centre DCM roll piezo")
            if not self._sleep(0.4): return self._abort_cleanup()
            self.substep_status.emit("3_3a", "done")

        # Defaults for everything a later substep reads, so any single step can
        # be switched off without leaving a name undefined further down.
        pitch_coarse = row["pitch"]
        roll_zero    = row["roll"]

        # 3b: Pitch scan → intensity peak (coarse, before roll)
        if not self._skip("3_3b"):
            self.substep_status.emit("3_3b", "running")
            self._pre_scan("3_3b")
            self.log("  Scanning DCM pitch → finding intensity peak (coarse)…")
            _true_peak_coarse = row["pitch"] + random.uniform(-0.01, 0.01)
            def _sim_pitch_coarse(a, b, n):
                return sim_scan_pitch(a, b, n, _true_peak_coarse)
            pitch_coarse, _sig3b = self._smart_scan_peak(
                pvs['pitch'],
                center=row["pitch"],
                half_range=(p["pitch_stop"] - p["pitch_start"]) / 2.0,
                steps=p["pitch_steps"],
                sim_fn=_sim_pitch_coarse,
                substep_key="3_3b",
            )
            if self._abort: return self._abort_cleanup()
            if pitch_coarse is None:
                pitch_coarse = row["pitch"]
                self.log("  INSUFFICIENT DATA: using table pitch value as fallback", "warn")
            self._write(pvs['pitch'], pitch_coarse, "3B — move pitch to the coarse peak")
            self.log(f"  Intensity peak at pitch = {pitch_coarse:.6f} → moved", "ok")
            self.substep_status.emit("3_3b", "waiting")
            if not self.request_confirm("3_3b"): return self._abort_cleanup()
            self.substep_status.emit("3_3b", "done")

        # 3c: Roll scan → BPM x = 0
        if not self._skip("3_3c"):
            self.substep_status.emit("3_3c", "running")
            self._pre_scan("3_3c")
            self.log("  Scanning DCM roll → finding BPM x = 0 zero-crossing…")
            roll_span  = p["roll_stop"] - p["roll_start"]
            roll_start = row["roll"] + p["roll_start"]
            # Put the simulated crossing inside the swept range; it used to sit at
            # absolute ~0, nowhere near a roll setpoint of about -7670.
            _true_zero = roll_start + abs(roll_span) * random.uniform(0.15, 0.45)
            def _sim_roll(x):
                return sim_zero_line(x, _true_zero, slope=10.0)
            roll_zero = self._scan_to_zero(
                pvs['roll'], pvs.get("bpm_x", ""),
                start=roll_start,
                step=roll_span / max(p["roll_steps"] - 1, 1),
                substep_key="3_3c",
                max_span=3.0 * abs(roll_span),
                sim_fn=_sim_roll,
                signal_label="BPM X",
            )
            if self._abort: return self._abort_cleanup()
            if roll_zero is None:
                roll_zero = row["roll"]
                self.log("  INSUFFICIENT DATA: using table roll value as fallback", "warn")
            self._write(pvs['roll'], roll_zero, "3C — move roll to the BPM x zero-crossing")
            self.log(f"  BPM x zero-crossing at roll = {roll_zero:.6f} → moved", "ok")
            self.substep_status.emit("3_3c", "waiting")
            if not self.request_confirm("3_3c"): return self._abort_cleanup()
            self.substep_status.emit("3_3c", "done")

        # 3d: Pitch scan → intensity peak (fine, after roll)
        if not self._skip("3_3d"):
            self.substep_status.emit("3_3d", "running")
            self._pre_scan("3_3d")
            self.log("  Scanning DCM pitch → finding intensity peak (fine)…")
            _true_peak = pitch_coarse + random.uniform(-0.005, 0.005)
            def _sim_pitch_fine(a, b, n):
                return sim_scan_pitch(a, b, n, _true_peak)
            pitch_peak, _sig3d = self._smart_scan_peak(
                pvs['pitch'],
                center=pitch_coarse,
                half_range=(p["pitch_stop"] - p["pitch_start"]) / 2.0,
                steps=p["pitch_steps"],
                sim_fn=_sim_pitch_fine,
                substep_key="3_3d",
            )
            if self._abort: return self._abort_cleanup()
            if pitch_peak is None:
                pitch_peak = pitch_coarse
                self.log("  INSUFFICIENT DATA: using coarse pitch value as fallback", "warn")
            self._write(pvs['pitch'], pitch_peak, "3D — move pitch to the fine peak")
            self.log(f"  Intensity peak at pitch = {pitch_peak:.6f} → moved", "ok")
            self.bpm_update.emit(roll_zero + random.uniform(-0.0005, 0.0005),
                                 random.uniform(-0.001, 0.001), 0.97)
            self.substep_status.emit("3_3d", "waiting")
            if not self.request_confirm("3_3d"): return self._abort_cleanup()
            self.substep_status.emit("3_3d", "done")
        self.step_status.emit(3, "done")
        self.log("Step 3 complete.", "ok")

        # Snapshot BPM y after DCM pitch alignment (3D)
        if self.simulate:
            self._scan_results["BPM Y @ 3D (µm)"] = "sim"
        else:
            _bpmy_3d = self._read_float(pvs.get("bpm_y", ""), "3D — snapshot BPM Y",
                                        allow_blank=True)
            self._scan_results["BPM Y @ 3D (µm)"] = (
                "—" if _bpmy_3d is None else f"{_bpmy_3d:.6g}")

        # Snapshot intensities before mirror goes in
        if self.simulate:
            self._scan_results["BPM Max Intensity w/o Mirror"]  = "sim"
            self._scan_results["MonP Max Intensity w/o Mirror"] = "sim"
        else:
            _bpm_i  = self._read_float(pvs.get("bpm_intensity", ""),
                                       "3D — snapshot BPM intensity (mirror out)",
                                       allow_blank=True)
            _monp_i = self._read_float(pvs.get("ion_chamber", ""),
                                       "3D — snapshot ion chamber (mirror out)",
                                       allow_blank=True)
            self._scan_results["BPM Max Intensity w/o Mirror"]  = (
                "—" if _bpm_i is None else f"{_bpm_i:.6g}")
            self._scan_results["MonP Max Intensity w/o Mirror"] = (
                "—" if _monp_i is None else f"{_monp_i:.6g}")

        # ── Step 4: Mirror alignment (optional) ───────────────────────
        if self.skip_mirror:
            self.step_status.emit(4, "done")
            self.log("━━ Step 4 — Mirror alignment skipped ━━", "warn")
        else:
            self._begin_chapter(4, "Mirror alignment")

            top_pv  = pvs.get("mir_slit_top", "")
            bot_pv  = pvs.get("mir_slit_bot", "")
            mir_piezo_pv = pvs.get("mir_piezo_pitch", "")
            signal_pv    = self._signal_pv("mir_signal")

            # Resolve VFM/VDM PVs from the mirror stages table
            vdm_pv, vfm_pv = self._mirror_yz_pvs()

            slit_size_a    = p.get("mir_slit_size_a", 0.1)
            slit_cen_start = p.get("mir_slit_cen_start", -2.0)
            slit_cen_stop  = p.get("mir_slit_cen_stop", 2.0)
            slit_cen_steps = int(p.get("mir_slit_cen_steps", 21))
            slit_size_b    = p.get("mir_slit_size_b", 0.2)
            slit_size_c    = p.get("mir_slit_size_c", 1.0)
            vdm_start      = p.get("mir_vdm_start", -500.0)
            vdm_stop       = p.get("mir_vdm_stop", 500.0)
            vdm_steps      = int(p.get("mir_vdm_steps", 21))
            vfm_start      = p.get("mir_vfm_start", -250.0)
            vfm_stop       = p.get("mir_vfm_stop", 250.0)
            vfm_steps      = int(p.get("mir_vfm_steps", 21))

            # ── 4A: Slit scan (mirror out) → find beam center ─────────
            # slit_peak used to be initialised inside 4A, so switching 4A off on
            # its own left 4C and the post-4E reopen with an undefined name.
            # With 4A off, work from wherever the slit actually is.
            if top_pv and bot_pv:
                slit_peak = (self._read_float(top_pv, "4 — read slit top position")
                             + self._read_float(bot_pv, "4 — read slit bottom position")) / 2.0
            else:
                slit_peak = 0.0

            if not self._skip("4_4A"):
                self.substep_status.emit("4_4A", "running")
                self._pre_scan("4_4A")
                self.log("  4A: Slit center scan — mirror out, finding beam center…")
                if top_pv and bot_pv:
                    cur_top = self._read_float(top_pv, "4A — read slit top position")
                    cur_bot = self._read_float(bot_pv, "4A — read slit bottom position")
                    cur_cen = (cur_top + cur_bot) / 2.0
                    self.log(f"  Closing slit to {slit_size_a} mm (center ≈ {cur_cen:.3f})")
                    self._write(top_pv, cur_cen + slit_size_a / 2.0, "4A — close slit top")
                    self._write(bot_pv, cur_cen - slit_size_a / 2.0, "4A — close slit bottom")
                    if not self._wait_motor_done(top_pv): return self._abort_cleanup()
                    if not self._wait_motor_done(bot_pv): return self._abort_cleanup()

                    xs_slit = np.linspace(cur_cen + slit_cen_start, cur_cen + slit_cen_stop, slit_cen_steps)
                    ys_slit = []
                    _true_cen = cur_cen + random.uniform(-0.3, 0.3)
                    for cen in xs_slit:
                        if self._abort: return self._abort_cleanup()
                        self._write(top_pv, cen + slit_size_a / 2.0, "4A — step slit top")
                        self._write(bot_pv, cen - slit_size_a / 2.0, "4A — step slit bottom")
                        if not self._wait_motor_done(top_pv): return self._abort_cleanup()
                        if not self._wait_motor_done(bot_pv): return self._abort_cleanup()
                        sig = (gaussian(cen, _true_cen, 0.5, 1000.0, 10.0) + random.uniform(-5, 5)
                               if self.simulate
                               else self._read_float(signal_pv, "4A — read slit scan signal"))
                        ys_slit.append(sig)
                        self.scan_point.emit("4_4A", float(cen), float(sig))
                        if not self._sleep(p["settle_time"]): return self._abort_cleanup()

                    slit_peak = find_peak_centroid(xs_slit, np.array(ys_slit))
                    self.scan_peak.emit("4_4A", slit_peak)
                    self.log(f"  Beam center at {slit_peak:.4f} mm → moving slit", "ok")
                    self._write(top_pv, slit_peak + slit_size_a / 2.0, "4A — centre slit top")
                    self._write(bot_pv, slit_peak - slit_size_a / 2.0, "4A — centre slit bottom")
                    if not self._wait_motor_done(top_pv): return self._abort_cleanup()
                    if not self._wait_motor_done(bot_pv): return self._abort_cleanup()
                else:
                    self.log("  Slit PVs not configured — skipping 4A slit scan.", "error")
                self.substep_status.emit("4_4A", "waiting")
                if not self.request_confirm("4_4A"): return self._abort_cleanup()
                self.substep_status.emit("4_4A", "done")

            # ── 4B: Mirror in + stripe selection ──────────────────────
            if not self._skip("4_4B"):
                self.substep_status.emit("4_4B", "running")
                self.log("  4B: Moving mirror into beam path…")
                for stage in self.mirror_stages:
                    if stage["pv"].strip():
                        self._write(stage["pv"], stage["val_in"],
                                    f"4B — move {stage['name']} IN")
                        self.log(f"  [{stage['pv']}] → {stage['val_in']}  ({stage['name']} IN)", "ok")
                        if not self._sleep(0.1): return self._abort_cleanup()
                if not self._wait_all_motors([st["pv"] for st in self.mirror_stages],
                                             "mirror stages"):
                    return self._abort_cleanup()
                self._mirror_in = True
                self.log("  Mirror in position.", "ok")
                if not self._apply_mirror_stripe(float(self.row.get("mono_e", 0))):
                    return self._abort_cleanup()
                self.substep_status.emit("4_4B", "done")

            # ── 4B2: Centre the mirror pitch piezo ─────────────────
            # Same pattern as 3A for the DCM: park the piezo mid-range (the
            # records report DRVL=-2, DRVH=12, so 5 really is the middle) and
            # then do the coarse work with the pitch motor.
            if not self._skip("4_4B2"):
                self.substep_status.emit("4_4B2", "running")
                if mir_piezo_pv:
                    centre = p["piezo_center"]
                    self._write(mir_piezo_pv, centre, "4B2 — centre the mirror pitch piezo")
                    self.log(f"  [{mir_piezo_pv}] → {centre}  (mirror piezo centred)", "ok")
                    if not self._sleep(0.3): return self._abort_cleanup()
                else:
                    self.log("  Mirror piezo pitch PV not configured — skipping 4B2.", "error")
                self.substep_status.emit("4_4B2", "done")

            # ── 4C: Close slit → pitch motor → BPMY = 0 ────────────
            # ── 4C: Close slit → pitch piezo → BPMY = 0 ──────────────
            if not self._skip("4_4C"):
                self.substep_status.emit("4_4C", "running")
                if top_pv and bot_pv:
                    self.log(f"  Narrowing slit to {slit_size_b} mm for mirror scan")
                    self._write(top_pv, slit_peak + slit_size_b / 2.0, "4C — narrow slit top")
                    self._write(bot_pv, slit_peak - slit_size_b / 2.0, "4C — narrow slit bottom")
                    if not self._wait_motor_done(top_pv): return self._abort_cleanup()
                    if not self._wait_motor_done(bot_pv): return self._abort_cleanup()

                mir_pitch_pv = (pvs.get("mir_pitch_motor") or "").strip()
                if mir_pitch_pv:
                    self._pre_scan("4_4C")
                    self.log("  Scanning mirror pitch motor → BPMY = 0…")
                    pitch_cur = self._read_float(mir_pitch_pv,
                                                 "4C — read mirror pitch motor position")
                    mpm_start = p.get("mir_pitch_start", -50.0)
                    mpm_stop  = p.get("mir_pitch_stop",   50.0)
                    mpm_steps = int(p.get("mir_pitch_steps", 21))
                    mpm_span  = mpm_stop - mpm_start
                    _pitch_zero = pitch_cur + mpm_start + abs(mpm_span) * random.uniform(0.15, 0.45)
                    def _sim_4c(x):
                        return sim_zero_line(x, _pitch_zero, slope=0.02, noise=0.002)
                    pitch_zero = self._scan_to_zero(
                        mir_pitch_pv, pvs.get("bpm_y", ""),
                        start=pitch_cur + mpm_start,
                        step=mpm_span / max(mpm_steps - 1, 1),
                        substep_key="4_4C",
                        max_span=3.0 * abs(mpm_span),
                        sim_fn=_sim_4c,
                        signal_label="BPM Y",
                    )
                    if pitch_zero is None: return self._abort_cleanup()
                    self._write(mir_pitch_pv, pitch_zero,
                                "4C — move the mirror pitch motor to the BPM Y zero-crossing")
                    if not self._wait_motor_done(mir_pitch_pv): return self._abort_cleanup()
                    self.log(f"  BPMY zero-crossing at pitch = {pitch_zero:.3f} µrad → moved", "ok")
                else:
                    self.log("  Mirror pitch motor PV not configured — skipping BPMY centering.", "error")
                self.substep_status.emit("4_4C", "waiting")
                if not self.request_confirm("4_4C"): return self._abort_cleanup()
                self.substep_status.emit("4_4C", "done")

            # ── 4D: Scan VDM:Y → find peak → move ────────────────────
            if not self._skip("4_4D"):
                self.substep_status.emit("4_4D", "running")
                self._pre_scan("4_4D")
                self.log("  4D: Scanning VDM:Y → finding signal peak…")
                vdm_cur = self._read_float(vdm_pv, "4D — read VDM:Y position")
                xs_vdm = np.linspace(vdm_cur + vdm_start, vdm_cur + vdm_stop, vdm_steps)
                ys_vdm = []
                _vdm_true = vdm_cur + random.uniform(-50, 50)
                for vdm_pos in xs_vdm:
                    if self._abort: return self._abort_cleanup()
                    self._write(vdm_pv, vdm_pos, "4D — step VDM:Y")
                    if not self._wait_motor_done(vdm_pv): return self._abort_cleanup()
                    sig = (gaussian(vdm_pos, _vdm_true, 150.0, 1000.0, 10.0) + random.uniform(-5, 5)
                           if self.simulate
                           else self._read_float(signal_pv, "4D — read VDM scan signal"))
                    ys_vdm.append(sig)
                    self.scan_point.emit("4_4D", float(vdm_pos), float(sig))
                    if not self._sleep(p["settle_time"]): return self._abort_cleanup()

                vdm_peak = find_peak_centroid(xs_vdm, np.array(ys_vdm))
                self.scan_peak.emit("4_4D", vdm_peak)
                _fwhm4d = fwhm_half_max(xs_vdm, np.array(ys_vdm))
                if _fwhm4d is not None:
                    self._scan_results["VDM Y FWHM @ 4D (µm)"] = f"{_fwhm4d:.2f}"
                self._write(vdm_pv, vdm_peak, "4D — move VDM:Y to the peak")
                if not self._wait_motor_done(vdm_pv): return self._abort_cleanup()
                self.log(f"  VDM:Y peak at {vdm_peak:.2f} → moved", "ok")
                self.substep_status.emit("4_4D", "waiting")
                if not self.request_confirm("4_4D"): return self._abort_cleanup()
                self.substep_status.emit("4_4D", "done")

            # ── 4E: Coupled VFM+VDM scan (VDM step = 2× VFM step) ────
            if not self._skip("4_4E"):
                self.substep_status.emit("4_4E", "running")
                self._pre_scan("4_4E")
                self.log("  4E: Coupled VFM:Y + VDM:Y scan (VDM step = 2× VFM step)…")
                vfm_cur  = self._read_float(vfm_pv, "4E — read VFM:Y position")
                vdm_ref  = self._read_float(vdm_pv, "4E — read VDM:Y reference position")
                xs_vfm   = np.linspace(vfm_cur + vfm_start, vfm_cur + vfm_stop, vfm_steps)
                ys_coupled = []
                _vfm_true = vfm_cur + random.uniform(-30, 30)
                for vfm_pos in xs_vfm:
                    if self._abort: return self._abort_cleanup()
                    vfm_delta = vfm_pos - vfm_cur
                    vdm_pos   = vdm_ref + 2.0 * vfm_delta
                    self._write(vfm_pv, vfm_pos, "4E — step VFM:Y")
                    self._write(vdm_pv, vdm_pos, "4E — step VDM:Y (2x VFM delta)")
                    if not self._wait_motor_done(vfm_pv): return self._abort_cleanup()
                    if not self._wait_motor_done(vdm_pv): return self._abort_cleanup()
                    sig = (gaussian(vfm_pos, _vfm_true, 80.0, 1000.0, 10.0) + random.uniform(-5, 5)
                           if self.simulate
                           else self._read_float(signal_pv, "4E — read coupled scan signal"))
                    ys_coupled.append(sig)
                    self.scan_point.emit("4_4E", float(vfm_pos), float(sig))
                    if not self._sleep(p["settle_time"]): return self._abort_cleanup()

                vfm_peak_pos    = find_peak_centroid(xs_vfm, np.array(ys_coupled))
                vfm_delta_final = vfm_peak_pos - vfm_cur
                vdm_final       = vdm_ref + 2.0 * vfm_delta_final
                self.scan_peak.emit("4_4E", vfm_peak_pos)
                _fwhm4e = fwhm_half_max(xs_vfm, np.array(ys_coupled))
                if _fwhm4e is not None:
                    self._scan_results["VFM Y FWHM @ 4E (µm)"] = f"{_fwhm4e:.2f}"
                self._write(vfm_pv, vfm_peak_pos, "4E — move VFM:Y to the peak")
                if not self._wait_motor_done(vfm_pv): return self._abort_cleanup()
                self._write(vdm_pv, vdm_final, "4E — move VDM:Y to the coupled position")
                if not self._wait_motor_done(vdm_pv): return self._abort_cleanup()
                self.log(f"  VFM:Y → {vfm_peak_pos:.2f}  VDM:Y → {vdm_final:.2f}  (2× delta applied)", "ok")
                self.substep_status.emit("4_4E", "waiting")
                if not self.request_confirm("4_4E"): return self._abort_cleanup()
                self.substep_status.emit("4_4E", "done")

            # ── 4F: Reopen the mirror slits ─────────────────────
            if not self._skip("4_4F"):
                self.substep_status.emit("4_4F", "running")
                if top_pv and bot_pv:
                    self.log(f"  4F: Opening the mirror slits to {slit_size_c} mm")
                    self._write(top_pv, slit_peak + slit_size_c / 2.0,
                                "4F — open slit top")
                    self._write(bot_pv, slit_peak - slit_size_c / 2.0,
                                "4F — open slit bottom")
                    if not self._wait_motor_done(top_pv): return self._abort_cleanup()
                    if not self._wait_motor_done(bot_pv): return self._abort_cleanup()
                    self.log(f"  Mirror slits open to {slit_size_c} mm.", "ok")
                else:
                    self.log("  Slit PVs not configured — skipping 4F.", "error")
                self.substep_status.emit("4_4F", "done")

            self.step_status.emit(4, "done")
            self.log("Step 4 complete.", "ok")

        # ── Step 5: Enable feedback loops ──────────────────────────────
        self._begin_chapter(5, "Enable feedback loops")

        # Insert the mirror if it is actually still out. Keying this on
        # skip_mirror was wrong once individual steps could be switched off:
        # disabling 4B alone left the mirror out with nothing putting it back.
        if not self._mirror_in:
            if not self._skip("5_5mir"):
                self.substep_status.emit("5_5mir", "running")
                self.log("  Mirror is out — moving it into the beam path now…")
                for stage in self.mirror_stages:
                    if stage["pv"].strip():
                        self._write(stage["pv"], stage["val_in"],
                                    f"5 — move {stage['name']} IN")
                        self.log(f"  [{stage['pv']}] → {stage['val_in']}  ({stage['name']} IN)", "ok")
                        if not self._sleep(0.1): return self._abort_cleanup()
                if not self._wait_all_motors([st["pv"] for st in self.mirror_stages],
                                             "mirror stages"):
                    return self._abort_cleanup()
                self._mirror_in = True
                self.log("  Mirror in position.", "ok")
                if not self._apply_mirror_stripe(float(self.row.get("mono_e", 0))):
                    return self._abort_cleanup()
                self.substep_status.emit("5_5mir", "done")

        # Close the JJC to its operating size before the feedback loops run.
        # The alignment itself is done with the JJC wide open at 4; 0.4 is the
        # operating value and must not be applied any earlier. This happens
        # whether Step 4 ran or was skipped, so the mirror is in either way.
        if not self._skip("5_5jjc"):
            self.substep_status.emit("5_5jjc", "running")
            jjc_pv  = self._jjc_size_pv()
            jjc_val = p.get("jjc_size_pre_feedback", 0.4)
            if jjc_pv:
                self._write(jjc_pv, jjc_val, "5 — close JJC slit before feedback")
                self.log(f"  [{jjc_pv}] → {jjc_val}  (JJC size, pre-feedback)", "ok")
                if not self._sleep(0.3): return self._abort_cleanup()
            else:
                self.log("  No \"JJC Size\" stage configured — skipping the JJC close.", "warn")
            self.substep_status.emit("5_5jjc", "done")

        if not self._skip("5_5a"):
            self.substep_status.emit("5_5a", "running")
            self.log(f"  Enabling H feedback: DCM piezo roll → BPM x = 0…")
            if not self._sleep(0.5): return self._abort_cleanup()
            self._write(pvs['feedback_h'], 1, "5A — enable H feedback")
            self.feedback_update.emit(True, False)
            self.bpm_update.emit(random.uniform(-0.0002, 0.0002),
                                 random.uniform(-0.001, 0.001), 0.97)
            self.log(f"  [{pvs['feedback_h']}] → 1  (H feedback ON)", "ok")
            self.substep_status.emit("5_5a", "done")

        if not self._skip("5_5b"):
            self.substep_status.emit("5_5b", "running")
            self._pre_scan("5_5b", allow_h=True)
            self.log("  Scanning DCM piezo pitch → max intensity…")
            dcm_piezo_pv = pvs.get("piezo_pitch", "")
            dp_start = p.get("dcm_piezo_start", -1.0)
            dp_stop  = p.get("dcm_piezo_stop",   1.0)
            dp_steps = int(p.get("dcm_piezo_steps", 21))
            if dcm_piezo_pv:
                piezo_cur = self._read_float(dcm_piezo_pv, "5B — read DCM pitch piezo position")
                xs_dcm = np.linspace(piezo_cur + dp_start, piezo_cur + dp_stop, dp_steps)
                ys_dcm = []
                _dcm_true = piezo_cur + random.uniform(-0.2, 0.2)
                for px in xs_dcm:
                    if self._abort: return self._abort_cleanup()
                    self._write(dcm_piezo_pv, px, "5B — step DCM pitch piezo")
                    if not self._sleep(p.get("piezo_settle_time", 0.2)): return self._abort_cleanup()
                    sig = (gaussian(px, _dcm_true, 0.3, 1000.0, 10.0) + random.uniform(-5, 5)
                           if self.simulate
                           else self._read_float(self._signal_pv("dcm_signal"),
                                                 "5B — read scan signal"))
                    ys_dcm.append(sig)
                    self.scan_point.emit("5_5b", float(px), float(sig))
                    self.bpm_update.emit(random.uniform(-0.0001, 0.0001),
                                         random.uniform(-0.001, 0.001), float(sig) / 1000.0)
                dcm_piezo_peak = find_peak_centroid(xs_dcm, np.array(ys_dcm))
                self.scan_peak.emit("5_5b", dcm_piezo_peak)
                self._write(dcm_piezo_pv, dcm_piezo_peak, "5B — move DCM piezo to the peak")
                self.log(f"  Intensity peak at DCM piezo = {dcm_piezo_peak:.5f} → moved", "ok")
            else:
                self.log("  DCM piezo pitch PV not configured — skipping.", "error")
            self.substep_status.emit("5_5b", "waiting")
            if not self.request_confirm("5_5b"): return self._abort_cleanup()
            self.substep_status.emit("5_5b", "done")

        if not self._skip("5_5c"):
            self.substep_status.emit("5_5c", "running")
            self._pre_scan("5_5c", allow_h=True)
            self.log("  Scanning mirror piezo pitch → BPM y = 0…")
            mir_piezo_pv5 = pvs.get("mir_piezo_pitch", "")
            mp_start = p.get("mir_piezo_start", -1.0)
            mp_stop  = p.get("mir_piezo_stop",   1.0)
            mp_steps = int(p.get("mir_piezo_steps", 21))
            if mir_piezo_pv5:
                piezo_cur5 = self._read_float(mir_piezo_pv5,
                                              "5C — read mirror pitch piezo position")
                mp_span  = mp_stop - mp_start
                _mp_zero = piezo_cur5 + random.uniform(-0.4, 0.4)
                def _sim_5c(x):
                    return sim_zero_line(x, _mp_zero, slope=0.5, noise=0.002)
                mp_zero = self._scan_to_zero(
                    mir_piezo_pv5, pvs.get("bpm_y", ""),
                    start=piezo_cur5 + mp_start,
                    step=mp_span / max(mp_steps - 1, 1),
                    substep_key="5_5c",
                    max_span=3.0 * abs(mp_span),
                    sim_fn=_sim_5c,
                    signal_label="BPM Y",
                )
                if mp_zero is None: return self._abort_cleanup()
                self._write(mir_piezo_pv5, mp_zero,
                            "5C — move mirror piezo to the BPM Y zero-crossing")
                self.log(f"  BPM y zero-crossing at mirror piezo = {mp_zero:.5f} → moved", "ok")
            else:
                self.log("  Mirror piezo pitch PV not configured — skipping.", "error")
            self.substep_status.emit("5_5c", "waiting")
            if not self.request_confirm("5_5c"): return self._abort_cleanup()
            self.substep_status.emit("5_5c", "done")

        if not self._skip("5_5d"):
            self.substep_status.emit("5_5d", "running")
            self.log(f"  Enabling V feedback: DCM piezo pitch → BPM y = 0…")
            if not self._sleep(0.4): return self._abort_cleanup()
            self._write(pvs['feedback_v'], 1, "5D — enable V feedback")
            self.feedback_update.emit(True, True)
            self.log(f"  [{pvs['feedback_v']}] → 1  (V feedback ON)", "ok")
            self.substep_status.emit("5_5d", "done")

        self.step_status.emit(5, "done")
        self.log("Step 5 complete.", "ok")
        self.log("━━ Alignment sequence finished successfully ━━", "ok")
        self.scan_results_ready.emit(dict(self._scan_results))
        self.finished.emit(True)

    def _apply_mirror_stripe(self, energy_kev):
        """Select mirror stripe based on energy. Checks RBV first; skips move if already within tolerance."""
        stripe = select_stripe(energy_kev)
        pos = _STRIPE_POSITIONS[stripe]
        vfm_rbv_pv = _STRIPE_VFM_X_PV + ".RBV"
        vdm_rbv_pv = _STRIPE_VDM_X_PV + ".RBV"
        if self.simulate:
            vfm_cur = float(self.epics.get(_STRIPE_VFM_X_PV) or 0.0)
            vdm_cur = float(self.epics.get(_STRIPE_VDM_X_PV) or 0.0)
        else:
            # A disconnected VFM:X used to read None -> 0.0, which matches the Si
            # stripe position, so the app announced "already on Si, no move
            # needed" while actually knowing nothing about the mirror.
            vfm_cur = self._read_float(vfm_rbv_pv, "mirror stripe — read VFM:X readback")
            vdm_cur = self._read_float(vdm_rbv_pv, "mirror stripe — read VDM:X readback")
        if abs(vfm_cur - pos["vfm_x"]) <= 10 and abs(vdm_cur - pos["vdm_x"]) <= 10:
            self.log(f"  Already on {stripe} stripe — no move needed", "ok")
            self.stripe_status.emit(stripe)
            self._scan_results["Mirror Stripe"] = stripe
            return True
        self.log(f"  Selecting mirror stripe: {stripe} (energy {energy_kev} keV)")
        self.stripe_status.emit("changing")
        self._write(_STRIPE_VFM_X_PV, pos["vfm_x"], f"mirror stripe — move VFM:X for {stripe}")
        self._write(_STRIPE_VDM_X_PV, pos["vdm_x"], f"mirror stripe — move VDM:X for {stripe}")
        if not self._wait_motor_done(_STRIPE_VFM_X_PV): return False
        if not self._wait_motor_done(_STRIPE_VDM_X_PV): return False
        self.log(f"  VFM:X → {pos['vfm_x']}  VDM:X → {pos['vdm_x']}  ({stripe} stripe)", "ok")
        self.stripe_status.emit(stripe)
        self._scan_results["Mirror Stripe"] = stripe
        return True

    def _abort_cleanup(self, reason="aborted by user"):
        self.log(f"Alignment {reason}.", "error")
        self.finished.emit(False)


# ─── Reusable widgets ─────────────────────────────────────────────────────────

def make_tag(text, color="grey"):
    lbl = QLabel(text)
    obj = {"green": "tag_green", "amber": "tag_amber",
           "red": "tag_red", "cyan": "tag_cyan"}.get(color, "tag_grey")
    lbl.setObjectName(obj)
    return lbl


def make_readout(label_text, value="—", unit="", value_color=None):
    """Returns (container_widget, value_label) so caller can update."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    lbl = QLabel(label_text.upper())
    lbl.setObjectName("readout_label")
    val = QLabel(f"{value}")
    val.setObjectName("readout_value")
    if value_color:
        val.setStyleSheet(f"color: {value_color};")
    unit_lbl = QLabel(unit)
    unit_lbl.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 10px;")
    row = QWidget()
    rh = QHBoxLayout(row)
    rh.setContentsMargins(0, 0, 0, 0)
    rh.setSpacing(4)
    rh.addWidget(val)
    rh.addWidget(unit_lbl)
    rh.addStretch()
    v.addWidget(lbl)
    v.addWidget(row)
    return w, val


def make_separator():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setStyleSheet(f"color: {PAL['border']};")
    return line


class NoScrollSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()

class NoScrollDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()

def styled_button(text, obj_name="", min_width=0):
    btn = QPushButton(text)
    if obj_name:
        btn.setObjectName(obj_name)
    if min_width:
        btn.setMinimumWidth(min_width)
    return btn


def style_plot(plot, title=None, x_label=None, y_label=None):
    """(Re-)apply the current PAL to a PlotWidget. Safe to call repeatedly.

    The labels have to be re-set rather than merely recoloured: pyqtgraph bakes
    the colour into the label HTML at setLabel() time, which is why the plots
    used to stay light after switching to a dark theme.
    """
    plot.setBackground(PAL["bg"])
    for axis in ("bottom", "left"):
        plot.getAxis(axis).setPen(pg.mkPen(color=PAL["border"]))
        plot.getAxis(axis).setTextPen(pg.mkPen(color=PAL["text_sec"]))
    if x_label is not None:
        plot.setLabel("bottom", x_label, color=PAL["text_dim"])
    if y_label is not None:
        plot.setLabel("left", y_label, color=PAL["text_dim"])
    plot.showGrid(x=True, y=True, alpha=0.15)
    if title is not None:
        plot.setTitle(title, color=PAL["text_sec"], size="11pt")
    return plot


def make_plot(title="", y_label="Signal", x_label="Motor position"):
    pg.setConfigOptions(antialias=True)
    return style_plot(pg.PlotWidget(), title or None, x_label, y_label)


class WrapHeader(QHeaderView):
    """A header view that word-wraps its section labels over several lines.

    The lookup table's columns are the union of every recorded key, so its
    headings ("MonP Max Intensity w/o Mirror") are far wider than the numbers
    underneath them. Qt's stock header paints one elided line, which leaves the
    operator guessing; this one wraps and grows tall enough to show every line.
    """

    _PAD_X = 6      # keeps the text clear of the draggable section divider
    _PAD_Y = 4

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        # Qt caches the header's size hint. Without dropping it on a resize the
        # header keeps its old height and clips the extra line that appears
        # when a column is dragged narrower.
        self.sectionResized.connect(self._invalidate_size_hint)

    def _section_text(self, logicalIndex):
        """The label Qt would have painted for this section ("" if none)."""
        model = self.model()
        if model is None:
            return ""
        text = model.headerData(logicalIndex, self.orientation(),
                                Qt.ItemDataRole.DisplayRole)
        return "" if text is None else str(text)

    def _invalidate_size_hint(self, *_args):
        """Force the header height to be recomputed after a column resize."""
        n = self.count()
        if n:
            self.headerDataChanged(self.orientation(), 0, n - 1)

    def paintSection(self, painter, rect, logicalIndex):
        """Draw the styled section background, then the wrapped label on top."""
        if not rect.isValid():
            return
        painter.save()
        opt = QStyleOptionHeader()
        opt.initFrom(self)
        opt.rect = rect
        opt.section = logicalIndex
        opt.orientation = self.orientation()
        opt.text = ""          # the style must not draw the label, we do
        # Going through the style keeps the app stylesheet's
        # QHeaderView::section rule (background, bottom border) in force.
        self.style().drawControl(QStyle.ControlElement.CE_HeaderSection,
                                 opt, painter, self)
        painter.setFont(self.font())
        painter.setPen(QColor(PAL["text_dim"]))
        painter.drawText(
            rect.adjusted(self._PAD_X, self._PAD_Y, -self._PAD_X, -self._PAD_Y),
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter,
            self._section_text(logicalIndex))
        painter.restore()

    def sectionSizeFromContents(self, logicalIndex):
        """Height tall enough for the label wrapped at the section's width."""
        size = super().sectionSizeFromContents(logicalIndex)
        text = self._section_text(logicalIndex)
        if not text:
            return size
        # Measure against the same inset rect paintSection draws into,
        # otherwise a line that only wraps once padded would be clipped.
        width = max(self.sectionSize(logicalIndex) - 2 * self._PAD_X, 1)
        box = QFontMetrics(self.font()).boundingRect(
            QRect(0, 0, width, 0),
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter, text)
        size.setHeight(max(size.height(), box.height() + 2 * self._PAD_Y))
        return size


# ─── Beam path widget ─────────────────────────────────────────────────────────
class BeamPathWidget(QWidget):
    ELEMENTS = [
        ("Source",  None),
        ("Xtal",    3),
        ("Mirror",  4),
        ("BPM",     5),
        ("Sample",  None),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step_status = {}
        self.setFixedHeight(70)
        self.setStyleSheet(f"background: {PAL['surface']}; border: 1px solid {PAL['border']}; border-radius: 6px;")

    def update_step(self, step, status):
        self._step_status[step] = status
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        n = len(self.ELEMENTS)
        el_w = 48
        gap = (W - n * el_w) // (n + 1)
        cx_start = gap + el_w // 2

        for i, (label, step) in enumerate(self.ELEMENTS):
            cx = cx_start + i * (el_w + gap)
            cy = H // 2 - 6

            active  = (step is None and i == 0) or (step and self._step_status.get(step) == "done")
            running = step and self._step_status.get(step) == "running"

            if running:
                border_col = QColor(PAL["amber"])
                fill_col   = QColor(PAL["cyan_dim"])
                text_col   = QColor(PAL["amber"])
            elif active:
                border_col = QColor(PAL["green"])
                fill_col   = QColor("#e6f4ea")
                text_col   = QColor(PAL["green"])
            else:
                border_col = QColor(PAL["border"])
                fill_col   = QColor(PAL["surface_hi"])
                text_col   = QColor(PAL["text_dim"])

            # Draw box
            painter.setBrush(fill_col)
            painter.setPen(QPen(border_col, 2))
            painter.drawRoundedRect(cx - 18, cy - 14, 36, 28, 4, 4)

            # Draw label
            painter.setPen(text_col)
            font = QFont("Consolas", 8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(cx - 18, cy - 14, 36, 28, Qt.AlignmentFlag.AlignCenter, label)

            # Draw connector line
            if i < n - 1:
                next_cx = cx_start + (i + 1) * (el_w + gap)
                line_col = QColor(PAL["green"]) if active else QColor(PAL["border"])
                painter.setPen(QPen(line_col, 2))
                painter.drawLine(cx + 18, cy, next_cx - 18, cy)

        # "Beam Path" label top-left
        painter.setPen(QColor(PAL["text_dim"]))
        font = QFont("Consolas", 8)
        painter.setFont(font)
        painter.drawText(8, 12, "BEAM PATH")


# ─── Log widget ───────────────────────────────────────────────────────────────
class LogWidget(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(200)

    def append_log(self, msg, level="info"):
        _colors = {"info": PAL["text_sec"], "ok": PAL["green"],
                   "warn": PAL["amber"],    "error": PAL["red"]}
        color = _colors.get(level, PAL["text_sec"])
        ts = time.strftime("%H:%M:%S")
        safe = html.escape(str(msg)).replace("  ", "&nbsp;&nbsp;")
        self.append(
            f'<span style="color:{PAL["text_dim"]}">{ts}</span> '
            f'<span style="color:{color}">{safe}</span>'
        )
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ─── PV fault dialog ─────────────────────────────────────────────────────────
class PVFaultDialog(QDialog):
    """Critical dialog shown while the alignment sequence is paused on a PV fault.

    Deliberately shown with show() rather than exec(): exec() starts a nested
    event loop, which would deliver further queued worker signals and let a
    second fault stack another dialog on top of this one. The sequence is
    genuinely stopped because the *worker thread* is blocked, so leaving the GUI
    responsive costs nothing and lets the operator inspect the Setup tab, the
    live readouts and the log while deciding.
    """

    retry_clicked = pyqtSignal()
    abort_clicked = pyqtSignal()

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CRITICAL — EPICS PV Fault")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(740, 480)
        self._check_future = None
        self._check_pool   = None
        self._check_timer  = None

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        hdr = QLabel("⛔  ALIGNMENT PAUSED — EPICS PV FAULT")
        hdr.setStyleSheet(
            f"background: {PAL['tag_red_bg']}; border: 1px solid {PAL['red']};"
            f" border-radius: 4px; color: {PAL['red']}; font-size: 14px;"
            f" font-weight: 700; padding: 8px 12px;"
        )
        lay.addWidget(hdr)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(150)
        lay.addWidget(self._detail)

        pick = QHBoxLayout()
        pick.addWidget(QLabel("Check PV:"))
        self._pv_combo = QComboBox()
        self._pv_combo.setMinimumWidth(300)
        pick.addWidget(self._pv_combo, 1)
        self.check_btn = styled_button("Check PV")
        self.check_btn.setToolTip("Connect to this PV now and report what it returns.\n"
                                  "Does not resume the sequence.")
        pick.addWidget(self.check_btn)
        lay.addLayout(pick)

        self._diag = QTextEdit()
        self._diag.setReadOnly(True)
        self._diag.setPlaceholderText("Press Check PV to test this PV for aliveness.")
        lay.addWidget(self._diag, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        self.retry_btn = styled_button("Try Again", "primary")
        self.retry_btn.setToolTip("Re-read the PV and, if it answers, carry on from here.")
        self.abort_btn = styled_button("Abort Alignment", "danger")
        btns.addWidget(self.retry_btn)
        btns.addWidget(self.abort_btn)
        lay.addLayout(btns)

        self.check_btn.clicked.connect(self._on_check)
        self.retry_btn.clicked.connect(self._on_retry)
        self.abort_btn.clicked.connect(self._on_abort)

        self.refresh(rows)

    # ── content ───────────────────────────────────────────────

    def refresh(self, rows):
        """Re-render in place. Never opens a second dialog."""
        blocks = []
        for pv, context, reason, when in rows:
            blocks.append(
                f'<div style="margin-bottom:10px">'
                f'<div style="color:{PAL["red"]};font-weight:700">'
                f'PV&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; {html.escape(pv)}</div>'
                f'<div style="color:{PAL["text_sec"]}">'
                f'Problem&nbsp; {html.escape(reason)}</div>'
                f'<div style="color:{PAL["text_sec"]}">'
                f'While&nbsp;&nbsp;&nbsp; {html.escape(context)}</div>'
                f'<div style="color:{PAL["text_dim"]}">'
                f'Detected&nbsp;{html.escape(when)}</div>'
                f'</div>'
            )
        self._detail.setHtml(
            '<div style="font-family:JetBrains Mono,Consolas,monospace;font-size:11px">'
            + "".join(blocks) + '</div>'
        )
        current = self._pv_combo.currentText()
        names = []
        for pv, _c, _r, _w in rows:
            if pv and pv not in names:
                names.append(pv)
        self._pv_combo.blockSignals(True)
        self._pv_combo.clear()
        self._pv_combo.addItems(names)
        idx = self._pv_combo.findText(current)
        self._pv_combo.setCurrentIndex(idx if idx >= 0 else max(0, len(names) - 1))
        self._pv_combo.blockSignals(False)

    def set_busy(self, busy):
        """Disable the action buttons once a choice has been made."""
        self.retry_btn.setEnabled(not busy)
        self.abort_btn.setEnabled(not busy)

    # ── Check PV ───────────────────────────────────────────

    def _on_check(self):
        pv = self._pv_combo.currentText().strip()
        if not pv:
            self._diag.setPlainText("No PV name to check — the read had no PV configured. "
                                    "Set it in the Setup tab.")
            return
        self._diag.setPlainText("Checking %s …" % pv)
        self.check_btn.setEnabled(False)
        self._check_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._check_future = self._check_pool.submit(describe_pv, pv)
        # Poll with a timer rather than spinning on processEvents(), which would
        # re-enter the event loop from inside a slot.
        self._check_timer = QTimer(self)
        self._check_timer.setInterval(100)
        self._check_timer.timeout.connect(self._poll_check)
        self._check_timer.start()

    def _poll_check(self):
        if self._check_future is None or not self._check_future.done():
            return
        self._check_timer.stop()
        try:
            text = self._check_future.result()
        except Exception as exc:
            text = "Check failed: %s" % exc
        self._diag.setPlainText(text)
        self.check_btn.setEnabled(True)
        try:
            self._check_pool.shutdown(wait=False)
        except Exception:
            pass
        self._check_future = None

    # ── actions ─────────────────────────────────────────────

    def _on_retry(self):
        self.set_busy(True)          # disable both first, so Retry-then-Abort cannot race
        self.retry_clicked.emit()

    def _on_abort(self):
        self.set_busy(True)
        self.abort_clicked.emit()


# ─── Setup Tab ───────────────────────────────────────────────────────────────
class SetupTab(QWidget):
    changed          = pyqtSignal()
    pv_readback      = pyqtSignal(str, str)   # (key, value_str)
    pv_disconnected  = pyqtSignal(str, str)   # (key, pv_name) — CA connection lost
    _string_refresh  = pyqtSignal(str)         # key — triggers Qt-thread string read

    _RBK_MONO = "font-family: 'JetBrains Mono',Consolas,monospace; font-size: 11px; padding: 0 6px; min-width: 88px;"

    @staticmethod
    def _rbk_style(variant: str) -> str:
        c = {"ok": PAL['cyan'], "err": PAL['amber'],
             "bad": PAL['red']}.get(variant, PAL['text_dim'])
        weight = "font-weight: 700;" if variant == "bad" else ""
        return f"color: {c}; {weight} {SetupTab._RBK_MONO}"

    def __init__(self, section=None, show_connection=True, parent=None):
        """section: one of PV_TABS' keys, or None for every field (the old
        single-tab behaviour, still used by the tests)."""
        super().__init__(parent)
        self._section            = section
        self._show_connection    = show_connection
        self._pv_fields          = {}
        self._scan_fields        = {}
        self._pv_value_labels    = {}
        self._monitored_pvs      = {}   # key → epics.PV object
        self._resubscribe_timers = {}   # key → QTimer (debounce)
        # PV names we are tearing down ourselves. pv.disconnect() fires the
        # connection callback, and without this the fault router would treat our
        # own re-subscribe as a hardware disconnect.
        self._resubscribing      = set()
        # Only one panel shows the Simulation checkbox; the others follow it.
        self._simulate_src       = None
        self._all_pvs_fn         = None    # Test EPICS should cover every panel
        self._full_save_fn       = None    # Save/Load Config write the whole file
        self._full_load_fn       = None
        self._string_refresh.connect(self._on_string_refresh)
        self._build()

    def _build(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_vlay = QVBoxLayout(inner)
        inner_vlay.setSpacing(12)
        inner_vlay.setContentsMargins(16, 16, 16, 16)
        top_w = QWidget()
        lay = QHBoxLayout(top_w)
        lay.setSpacing(16)
        lay.setContentsMargins(0, 0, 0, 0)

        # PV names — left column has two stacked group boxes
        pv_col = QVBoxLayout()
        keep_pv   = None if self._section is None else set(PV_TABS[self._section])
        keep_scan = None if self._section is None else set(SCAN_TABS[self._section])

        motor_labels = {
            "mono_energy":   "Mono Energy",
            "roll":          "DCM Roll",
            "pitch":         "DCM Pitch",
            "mir_slit_top":  "Mirror Slit Top",
            "mir_slit_bot":  "Mirror Slit Bottom",
        }
        motor_labels = {k: v for k, v in motor_labels.items()
                        if keep_pv is None or k in keep_pv}
        motor_box = QGroupBox("Motor PVs")
        motor_lay = QGridLayout(motor_box)
        motor_lay.setSpacing(8)
        motor_lay.setColumnStretch(1, 1)
        hdr_rbv = QLabel("RBV")
        hdr_rbv.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 10px; letter-spacing: 1px;")
        motor_lay.addWidget(hdr_rbv, 0, 2, Qt.AlignmentFlag.AlignHCenter)
        for row, (key, label) in enumerate(motor_labels.items()):
            motor_lay.addWidget(QLabel(label), row, 0)
            ed = QLineEdit(DEFAULT_PVS[key])
            self._pv_fields[key] = ed
            motor_lay.addWidget(ed, row, 1)
            rbk = QLabel("—")
            rbk.setStyleSheet(self._rbk_style("dim"))
            rbk.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._pv_value_labels[key] = rbk
            motor_lay.addWidget(rbk, row, 2)

        other_labels = {
            # Undulator
            "und_energy":      "Undulator Energy",
            "und_harmonic":    "Undulator Harmonic",
            "und_start":       "Undulator Start",
            "und_busy":        "Undulator Busy flag",
            "und_energy_rbv":  "Undulator Energy RBV",
            # DCM Piezos
            "piezo_pitch":     "DCM Piezo Pitch",
            "piezo_roll":      "DCM Piezo Roll",
            # BPM
            "bpm_x":           "BPM X readback",
            "bpm_y":           "BPM Y readback",
            "bpm_intensity":   "BPM Intensity",
            "bpm_sen":         "BPM Sensitivity",
            # Feedback
            "feedback_h":      "H Feedback PV",
            "feedback_v":      "V Feedback PV",
            "auto_feedback":   "Auto Feedback On",
            "shutter_blocking": "Upstream Shutter (blocking)",
            # Mirror
            "mir_piezo_pitch": "Mirror Piezo Pitch",
            "mir_pitch_motor": "Mirror Pitch Motor",
            "mir_slit_center": "Mirror Slit Center",
            "mir_slit_size":   "Mirror Slit Size",
            # Ion Chamber
            "ion_chamber":     "Ion Chamber",
            "ic_sen_unit":     "Ion Chamber Sen Unit",
            "ic_sen_num":      "Ion Chamber Sen Num",
        }
        other_labels = {k: v for k, v in other_labels.items()
                        if keep_pv is None or k in keep_pv}
        other_box = QGroupBox("Other PVs")
        other_lay = QGridLayout(other_box)
        other_lay.setSpacing(8)
        other_lay.setColumnStretch(1, 1)
        hdr_rbv2 = QLabel("Value")
        hdr_rbv2.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 10px; letter-spacing: 1px;")
        other_lay.addWidget(hdr_rbv2, 0, 2, Qt.AlignmentFlag.AlignHCenter)
        for row, (key, label) in enumerate(other_labels.items()):
            other_lay.addWidget(QLabel(label), row, 0)
            ed = QLineEdit(DEFAULT_PVS[key])
            self._pv_fields[key] = ed
            other_lay.addWidget(ed, row, 1)
            rbk = QLabel("—")
            rbk.setStyleSheet(self._rbk_style("dim"))
            rbk.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._pv_value_labels[key] = rbk
            other_lay.addWidget(rbk, row, 2)

        motor_box.setVisible(bool(motor_labels))
        other_box.setVisible(bool(other_labels))
        pv_col.addWidget(motor_box)
        pv_col.addWidget(other_box)
        pv_col.addStretch()

        # Right column
        right = QVBoxLayout()

        scan_box = QGroupBox("Scan Parameters")
        scan_lay = QGridLayout(scan_box)
        # Which detector the DCM pitch/piezo scans read. The code used to look up
        # a "i0" PV key that exists in neither DEFAULT_PVS nor the saved config,
        # so on hardware it read a PV literally named "I0" (or "").
        if keep_scan is None or "dcm_signal" in keep_scan:
            scan_lay.addWidget(QLabel("DCM scan signal"), 0, 0)
            dcm_sig_cb = QComboBox()
            dcm_sig_cb.addItems(["BPM Intensity", "Ion Chamber"])
            dcm_sig_cb.setToolTip("Detector read by the Step 3 pitch scans and the "
                                  "Step 5B DCM piezo scan.")
            self._scan_fields["dcm_signal"] = dcm_sig_cb
            scan_lay.addWidget(dcm_sig_cb, 0, 1)
        scan_defs = [
            ("pitch_start",  "Pitch scan start",  QDoubleSpinBox, -1000.0, 0.0, -0.05, 4),
            ("pitch_stop",   "Pitch scan stop",   QDoubleSpinBox,     0.0, 1000.0,  0.05, 4),
            ("pitch_steps",  "Pitch scan steps",  QSpinBox,        5, 200,   25,   0),
            ("roll_start",   "Roll scan start",   QDoubleSpinBox, -1000.0, 0.0, -0.05, 4),
            ("roll_stop",    "Roll scan stop",    QDoubleSpinBox,     0.0, 1000.0,  0.05, 4),
            ("roll_steps",   "Roll scan steps",   QSpinBox,        5, 200,   21,   0),
            ("settle_time",        "Settle time (s)",        QDoubleSpinBox, 0.0, 5.0, 0.1, 2),
            ("piezo_settle_time",  "Piezo settle time (s)",  QDoubleSpinBox, 0.0, 5.0, 0.2, 2),
            ("pv_ack_timeout",      "PV write ack timeout (s)",   QDoubleSpinBox, 1.0, 120.0, 10.0, 1),
            ("motor_start_grace",   "Motor start grace (s)",      QDoubleSpinBox, 0.5, 120.0,  5.0, 1),
            ("motor_stall_timeout", "Motor stall timeout (s)",    QDoubleSpinBox, 1.0, 600.0, 30.0, 1),
            ("piezo_center", "Piezo center value",QDoubleSpinBox,  0.0, 10.0, 5.0,  1),
            ("smart_edge_fraction",    "Smart scan edge fraction",   QDoubleSpinBox, 0.05, 0.5,  0.2, 2),
            ("smart_max_extend_steps", "Smart scan max extend steps",QSpinBox,       1,    50,   10,  0),
            ("smart_fine_sigma_range", "Fine scan range (σ)",        QDoubleSpinBox, 0.5,  10.0, 2.0, 1),
            ("smart_fine_scan_iter",   "Fine scan iterations",       QSpinBox,       1,    10,   3,   0),
            ("dcm_piezo_start",  "DCM pitch piezo scan start",    QDoubleSpinBox, -1000.0, 0.0,  -1.0, 3),
            ("dcm_piezo_stop",   "DCM pitch piezo scan stop",     QDoubleSpinBox,     0.0, 1000.0, 1.0, 3),
            ("dcm_piezo_steps",  "DCM pitch piezo scan steps",    QSpinBox,        3, 200,   21,   0),
            ("mir_piezo_start",  "Mirror pitch piezo scan start", QDoubleSpinBox, -1000.0, 0.0,  -1.0, 3),
            ("mir_piezo_stop",   "Mirror pitch piezo scan stop",  QDoubleSpinBox,     0.0, 1000.0, 1.0, 3),
            ("mir_piezo_steps",  "Mirror pitch piezo scan steps", QSpinBox,        3, 200,   21,   0),
        ]
        scan_defs = [d for d in scan_defs if keep_scan is None or d[0] in keep_scan]
        for r, (key, lbl, cls, mn, mx, dflt, dec) in enumerate(scan_defs, start=1):
            scan_lay.addWidget(QLabel(lbl), r, 0)
            if cls == QDoubleSpinBox:
                sb = NoScrollDoubleSpinBox()
                sb.setDecimals(dec)
                sb.setRange(mn, mx)
                sb.setValue(dflt)
            else:
                sb = NoScrollSpinBox()
                sb.setRange(mn, mx)
                sb.setValue(dflt)
            sb.setMinimumWidth(100)
            self._scan_fields[key] = sb
            scan_lay.addWidget(sb, r, 1)

        conn_box = QGroupBox("Connection")
        conn_lay = QVBoxLayout(conn_box)
        self.sim_check = QCheckBox("Simulation mode (no EPICS required)")
        self.sim_check.setChecked(True)
        if not EPICS_AVAILABLE:
            self.sim_check.setChecked(True)
            self.sim_check.setEnabled(False)
            self.sim_check.setToolTip("pyepics not installed — simulation only")
        conn_lay.addWidget(self.sim_check)

        btn_row = QHBoxLayout()
        test_btn = styled_button("Test EPICS Connection")
        test_btn.clicked.connect(self._test_epics)
        load_btn = styled_button("Load Config…")
        save_btn = styled_button("Save Config…")
        load_btn.clicked.connect(self._load_config)
        save_btn.clicked.connect(self._save_config)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(load_btn)
        btn_row.addWidget(save_btn)
        btn_row.addStretch()
        conn_lay.addLayout(btn_row)

        epics_note = QLabel(
            "pyepics available ✓" if EPICS_AVAILABLE
            else "pyepics not installed — running in simulation mode"
        )
        epics_note.setStyleSheet(f"color: {PAL['green'] if EPICS_AVAILABLE else PAL['amber']}; font-size: 11px;")
        conn_lay.addWidget(epics_note)

        scan_box.setVisible(bool(self._scan_fields))
        conn_box.setVisible(self._show_connection)
        right.addWidget(scan_box)
        right.addWidget(conn_box)
        right.addStretch()

        lay.addLayout(pv_col, 2)
        lay.addLayout(right, 1)
        inner_vlay.addWidget(top_w)

        scroll.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        for ed in self._pv_fields.values():
            ed.textChanged.connect(self.changed)
        for w in self._scan_fields.values():
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                w.valueChanged.connect(self.changed)
            elif isinstance(w, QComboBox):
                w.currentTextChanged.connect(self.changed)
        self.sim_check.toggled.connect(self.changed)
        self.sim_check.toggled.connect(self._on_sim_toggled)

        # Live readback via EPICS monitors (callbacks → Qt signal → label update)
        self.pv_readback.connect(self._on_pv_readback)
        # Debounce re-subscribe when a PV name is edited
        for key, ed in self._pv_fields.items():
            ed.textChanged.connect(lambda _text, k=key: self._schedule_resubscribe(k))
        if EPICS_AVAILABLE and not self.is_simulate():
            self._start_monitoring()

    # ── sim-mode toggle ──────────────────────────────────────────────────────
    def _on_sim_toggled(self, is_sim: bool):
        if is_sim or not EPICS_AVAILABLE:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    # ── EPICS monitor management ─────────────────────────────────────────────
    def _start_monitoring(self):
        for key in self._pv_fields:
            self._subscribe_pv(key)

    def _stop_monitoring(self):
        for pv in self._monitored_pvs.values():
            try:
                self._mark_resubscribing(getattr(pv, "pvname", ""))
                pv.disconnect()
            except Exception:
                pass
        self._monitored_pvs.clear()
        for lbl in self._pv_value_labels.values():
            lbl.setText("—")
            lbl.setStyleSheet(self._rbk_style("dim"))

    def _mark_resubscribing(self, pv_name):
        """Suppress the disconnect callback caused by our own teardown."""
        name = (pv_name or "").strip()
        if not name:
            return
        self._resubscribing.add(name)
        # Outlive the router's 2 s debounce.
        QTimer.singleShot(3000, lambda n=name: self._resubscribing.discard(n))

    def _subscribe_pv(self, key: str):
        # Tear down any existing subscription for this key
        old = self._monitored_pvs.pop(key, None)
        if old is not None:
            try:
                self._mark_resubscribing(getattr(old, "pvname", ""))
                old.disconnect()
            except Exception:
                pass

        pv_name = self._pv_fields[key].text().strip()
        if not pv_name or self.is_simulate() or not EPICS_AVAILABLE:
            self.pv_readback.emit(key, "—")
            return

        # Motors: monitor .RBV field
        actual = (pv_name + ".RBV") if key in MOTOR_PV_KEYS else pv_name

        _string_keys = {"bpm_sen", "ic_sen_unit", "ic_sen_num"}

        def _value_cb(value=None, char_value=None, **_kw):
            if value is None:
                return
            if key in _string_keys:
                # Emit signal to do pv.get(as_string=True) on the Qt thread
                self._string_refresh.emit(key)
                return
            try:
                if isinstance(value, (int, float)):
                    self.pv_readback.emit(key, f"{float(value):.6g}")
                else:
                    # numpy scalar or array
                    v = float(value) if hasattr(value, '__float__') else str(value)[:16]
                    self.pv_readback.emit(key, f"{v:.6g}" if isinstance(v, float) else v)
            except Exception:
                self.pv_readback.emit(key, str(char_value or value)[:16])

        def _conn_cb(pvname=None, conn=None, **_kw):
            # Runs on a libca callback thread: emit signals, never touch widgets.
            if not conn:
                self.pv_readback.emit(key, "DISCONNECTED")
                self.pv_disconnected.emit(key, actual)
            elif key in _string_keys:
                self._string_refresh.emit(key)

        import epics as _epics
        try:
            pv = _epics.PV(actual, callback=_value_cb,
                           connection_callback=_conn_cb, auto_monitor=True)
            self._monitored_pvs[key] = pv
        except Exception:
            self.pv_readback.emit(key, "err")

    def _schedule_resubscribe(self, key: str):
        """Debounce PV name edits: wait 1 s of inactivity before re-subscribing."""
        if not EPICS_AVAILABLE or self.is_simulate():
            return
        t = self._resubscribe_timers.get(key)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda k=key: self._subscribe_pv(k))
            self._resubscribe_timers[key] = t
        t.start(1000)

    def _on_string_refresh(self, key: str):
        pv = self._monitored_pvs.get(key)
        if pv is None:
            return
        try:
            val = pv.get(as_string=True)
            if val is not None:
                self.pv_readback.emit(key, str(val)[:16])
        except Exception:
            pass

    def _on_pv_readback(self, key: str, value: str):
        lbl = self._pv_value_labels.get(key)
        if lbl is None:
            return
        if value == "DISCONNECTED":
            # Previously shown as a dim "n/c", indistinguishable from a PV that
            # was simply never configured.
            lbl.setStyleSheet(self._rbk_style("bad"))
            lbl.setToolTip("Channel access reports this PV as disconnected.")
        elif value == "err":
            lbl.setStyleSheet(self._rbk_style("err"))
            lbl.setToolTip("Could not subscribe to this PV.")
        elif value == "—":
            lbl.setStyleSheet(self._rbk_style("dim"))
            lbl.setToolTip("")
        else:
            lbl.setStyleSheet(self._rbk_style("ok"))
            lbl.setToolTip("")
        lbl.setText(value)

    def _test_epics(self):
        if not EPICS_AVAILABLE:
            QMessageBox.warning(self, "Test EPICS Connection",
                                "pyepics is not installed — cannot test connections.")
            return
        pvs = (self._all_pvs_fn or self.get_pvs)()
        timeout = 2.0

        def _check(key, pv_name):
            if not pv_name.strip():
                return key, (pv_name, "skipped")
            _ok, detail = probe_pv(pv_name, timeout=timeout,
                                   try_rbv=(key in MOTOR_PV_KEYS))
            return key, (pv_name, detail)

        # Pre-seed so the dialog lists PVs in Setup-field order, not completion
        # order, and so a crashed probe still shows a row.
        results = {k: (v, "timeout") for k, v in pvs.items()}
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(len(pvs), 1)) as ex:
                pending = {ex.submit(_check, k, v) for k, v in pvs.items()}
                while pending:
                    done, pending = concurrent.futures.wait(pending, timeout=0.05)
                    QApplication.processEvents()   # keep the window repainting
                    for f in done:
                        k, val = f.result()
                        results[k] = val
        finally:
            QApplication.restoreOverrideCursor()

        labels = {
            "mono_energy": "Mono Energy", "roll": "DCM Roll", "pitch": "DCM Pitch",
            "mir_slit_top": "Mirror Slit Top", "mir_slit_bot": "Mirror Slit Bottom",
            "und_energy": "Undulator Energy",
            "piezo_pitch": "DCM Piezo Pitch", "piezo_roll": "DCM Piezo Roll",
            "bpm_x": "BPM X", "bpm_y": "BPM Y", "bpm_intensity": "BPM Intensity",
            "feedback_h": "H Feedback", "feedback_v": "V Feedback",
            "und_harmonic": "Und Harmonic", "und_start": "Und Start",
            "mir_piezo_pitch": "Mirror Piezo Pitch", "ion_chamber": "Ion Chamber",
        }
        n_ok = sum(1 for _, (_, s) in results.items() if s in ("ok", "ok (via .RBV)"))
        n_total = sum(1 for _, (pv, s) in results.items() if s != "skipped")

        rows = ""
        for key, (pv_name, status) in results.items():
            label = labels.get(key, key)
            if status in ("ok", "ok (via .RBV)"):
                icon, color = "✓", PAL["green"]
            elif status == "skipped":
                icon, color = "—", PAL["text_dim"]
            else:
                icon, color = "✗", PAL["red"]
            rows += (
                f'<tr>'
                f'<td style="padding:3px 8px;color:{PAL["text_sec"]}">{label}</td>'
                f'<td style="padding:3px 8px;font-family:monospace;color:{PAL["text_dim"]}">{pv_name or "(empty)"}</td>'
                f'<td style="padding:3px 8px;color:{color};font-weight:600">{icon} {status}</td>'
                f'</tr>'
            )

        dlg = QDialog(self)
        dlg.setWindowTitle("EPICS Connection Test")
        dlg.setMinimumWidth(560)
        layout = QVBoxLayout(dlg)
        summary = QLabel(f"<b>{n_ok} / {n_total} PVs connected</b>")
        summary.setStyleSheet(f"font-size:13px; color:{PAL['green'] if n_ok == n_total else PAL['amber']};")
        layout.addWidget(summary)
        text = QLabel(f'<table cellspacing="0">{rows}</table>')
        text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(text)
        close_btn = styled_button("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()

    def _save_config(self):
        if self._full_save_fn is not None:
            return self._full_save_fn()
        path, _ = QFileDialog.getSaveFileName(self, "Save Config", "dcm_config.json", "JSON files (*.json)")
        if not path:
            return
        cfg = {
            "pvs": self.get_pvs(),
            "scan": self.get_scan_params(),
            "simulate": self.is_simulate(),
        }
        try:
            with open(path, "w") as f:
                json.dump(cfg, f, indent=2)
        except OSError as e:
            QMessageBox.critical(self, "Save Config", f"Could not write file:\n{e}")

    def _apply_config(self, cfg):
        """Apply a saved config. Returns the keys that could not be applied."""
        bad = []
        for k, v in cfg.get("pvs", {}).items():
            if k in self._pv_fields:
                self._pv_fields[k].setText("" if v is None else str(v))
        for k, v in cfg.get("scan", {}).items():
            if k not in self._scan_fields:
                continue
            w = self._scan_fields[k]
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                if not set_spin_value(w, v):
                    bad.append(k)
            elif isinstance(w, QComboBox):
                idx = w.findText(str(v))
                if idx >= 0:
                    w.setCurrentIndex(idx)
                else:
                    bad.append(k)
        if "simulate" in cfg:
            self.sim_check.setChecked(bool(cfg["simulate"]))
        return bad

    def _load_config(self):
        if self._full_load_fn is not None:
            return self._full_load_fn()
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Load Config", f"Could not read file:\n{e}")
            return
        self._apply_config(cfg)

    def get_pvs(self):
        return {k: v.text() for k, v in self._pv_fields.items()}

    def get_scan_params(self):
        out = {}
        for k, w in self._scan_fields.items():
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                out[k] = w.value()
            elif isinstance(w, QComboBox):
                out[k] = w.currentText()
        return out

    def is_simulate(self):
        if self._simulate_src is not None:
            return self._simulate_src()
        return self.sim_check.isChecked()

    def set_simulate_source(self, fn):
        """Follow another panel's Simulation checkbox instead of our own."""
        self._simulate_src = fn

    def set_shared_hooks(self, all_pvs_fn=None, save_fn=None, load_fn=None):
        """Let MainWindow supply the whole-application view of things.

        Without this, Test EPICS would only probe this panel's PVs and
        "Save Config..." would write a three-key file over the real one.
        """
        self._all_pvs_fn   = all_pvs_fn
        self._full_save_fn = save_fn
        self._full_load_fn = load_fn


# ─── Energy Table Tab ─────────────────────────────────────────────────────────
class EnergyTableTab(QWidget):
    row_selected = pyqtSignal(dict)
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data     = [dict(r) for r in DEFAULT_LOOKUP]
        self._selected = None
        self._records  = []
        self._col_widths = {}     # header label -> px, see _refresh_lookup_table
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        # ── Top: energy table ──
        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 8)
        top_lay.setSpacing(8)

        bar = QHBoxLayout()
        add_btn    = styled_button("+ Add Row")
        del_btn    = styled_button("Remove Row")
        imp_btn    = styled_button("Import CSV…")
        exp_btn    = styled_button("Export CSV…")
        sort_lbl   = QLabel("Sort by:")
        sort_lbl.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 11px;")
        self._sort_combo = QComboBox()
        self._sort_combo.addItems(["Mono E (keV)", "Undulator E (keV)", "Harmonic", "Roll", "Pitch"])
        self._sort_combo.setFixedWidth(140)
        sort_btn = styled_button("↑ Sort")
        sort_btn.setFixedWidth(60)
        for b in [add_btn, del_btn, imp_btn, exp_btn]:
            bar.addWidget(b)
        bar.addSpacing(12)
        bar.addWidget(sort_lbl)
        bar.addWidget(self._sort_combo)
        bar.addWidget(sort_btn)
        bar.addStretch()
        self.sel_label = QLabel("No row selected")
        self.sel_label.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 11px;")
        bar.addWidget(self.sel_label)
        top_lay.addLayout(bar)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "Mono E (keV)", "Undulator E (keV)", "Harmonic", "Roll", "Pitch",
            "BPM Sen", "IC Sen Unit", "IC Sen Num",
        ])
        # Interactive rather than Stretch: Stretch owns every width itself, so
        # the dividers cannot be dragged at all.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.cellChanged.connect(self._on_cell_changed)
        top_lay.addWidget(self.table)

        add_btn.clicked.connect(self._add_row)
        del_btn.clicked.connect(self._del_row)
        imp_btn.clicked.connect(self._import_csv)
        exp_btn.clicked.connect(self._export_csv)
        sort_btn.clicked.connect(self._sort_rows)

        self._refresh_table()
        splitter.addWidget(top)

        # ── Bottom: lookup table ──
        bot_box = QGroupBox("Lookup Table")
        bot_lay = QVBoxLayout(bot_box)

        lk_bar = QHBoxLayout()
        lk_imp_btn  = styled_button("Import CSV…")
        lk_exp_btn  = styled_button("Export CSV…")
        lk_del_btn  = styled_button("Remove Row")
        lk_clr_btn  = styled_button("Clear All")
        for b in [lk_imp_btn, lk_exp_btn, lk_del_btn, lk_clr_btn]:
            lk_bar.addWidget(b)
        lk_bar.addStretch()
        bot_lay.addLayout(lk_bar)

        self._lookup_table = QTableWidget()
        self._lookup_table.setAlternatingRowColors(True)
        self._lookup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._lookup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Header setup belongs here and not in _refresh_lookup_table, which runs
        # on every appended record: replacing the header object or re-applying
        # the resize mode there would throw the operator's widths away.
        self._lookup_table.setHorizontalHeader(
            WrapHeader(Qt.Orientation.Horizontal, self._lookup_table))
        lk_head = self._lookup_table.horizontalHeader()
        lk_head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        lk_head.setStretchLastSection(True)
        lk_head.setMinimumSectionSize(64)   # a wrapped heading needs some width
        bot_lay.addWidget(self._lookup_table)

        lk_imp_btn.clicked.connect(self._import_lookup_csv)
        lk_exp_btn.clicked.connect(self._export_lookup_csv)
        lk_del_btn.clicked.connect(self._del_record_row)
        lk_clr_btn.clicked.connect(self._clear_records)

        self._refresh_lookup_table()
        splitter.addWidget(bot_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        lay.addWidget(splitter)

    _COLS = ["mono_e", "ue", "harmonic", "roll", "pitch", "bpm_sen", "ic_sen_unit", "ic_sen_num"]
    _SORT_KEYS = ["mono_e", "ue", "harmonic", "roll", "pitch"]

    def _refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._data))
        for r, row in enumerate(self._data):
            for c, key in enumerate(self._COLS):
                val = row.get(key, "")
                item = QTableWidgetItem(str(val))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)

    def _on_cell_changed(self, r, c):
        if r >= len(self._data):
            return
        key = self._COLS[c]
        item = self.table.item(r, c)
        if item is None:
            return
        text = item.text()
        if key in ("bpm_sen", "ic_sen_unit", "ic_sen_num"):
            self._data[r][key] = text
            self.changed.emit()
            return
        try:
            val = int(text) if key == "harmonic" else float(text)
            self._data[r][key] = val
            self.changed.emit()
        except ValueError:
            pass

    def _sort_rows(self):
        idx = self._sort_combo.currentIndex()
        key = self._SORT_KEYS[idx] if idx < len(self._SORT_KEYS) else "mono_e"

        def sort_key(r):
            # A mixed str/float column would otherwise raise
            # TypeError: '<' not supported between instances of 'str' and 'float'.
            try:
                return (0, float(r.get(key, 0)))
            except (TypeError, ValueError):
                return (1, 0.0)

        self._data.sort(key=sort_key)
        self._refresh_table()
        self.changed.emit()

    def _on_select(self):
        rows = self.table.selectedItems()
        if not rows:
            self._selected = None
            self.sel_label.setText("No row selected")
            return
        r = self.table.currentRow()
        if r < 0 or r >= len(self._data):
            self._selected = None
            self.sel_label.setText("No row selected")
            return
        self._selected = self._data[r]
        self.sel_label.setText(
            f"Selected: MonoE = {self._selected['mono_e']} keV  |  UE = {self._selected['ue']} keV"
        )
        self.sel_label.setStyleSheet(f"color: {PAL['cyan']}; font-size: 11px; font-family: 'JetBrains Mono', monospace;")
        self.row_selected.emit(self._selected)

    def _add_row(self):
        if self._data:
            new_row = coerce_energy_row(self._data[-1])
        else:
            new_row = coerce_energy_row({})
        self._data.append(new_row)
        self._refresh_table()
        self.table.selectRow(len(self._data) - 1)
        self.changed.emit()

    def _del_row(self):
        r = self.table.currentRow()
        if r < 0:
            return
        self._data.pop(r)
        self._refresh_table()
        self.changed.emit()

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import CSV", "", "CSV files (*.csv)")
        if not path:
            return
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            self._data = []
            for row in reader:
                try:
                    mono_e = float(row.get("mono_e", 0))
                except (TypeError, ValueError):
                    continue   # not a data row
                entry = dict(row)
                if not str(row.get("harmonic", "")).strip():
                    entry["harmonic"] = calc_harmonic(mono_e)
                self._data.append(coerce_energy_row(entry))
        self._refresh_table()
        self.changed.emit()

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Energy Table CSV", "energy_table.csv", "CSV files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["mono_e", "ue", "harmonic", "roll", "pitch",
                                                    "bpm_sen", "ic_sen_unit", "ic_sen_num"])
            writer.writeheader()
            writer.writerows(self._data)

    def selected_row(self):
        return self._selected

    def get_table_data(self):
        return [dict(r) for r in self._data]

    def set_table_data(self, data):
        self._data = [coerce_energy_row(r) for r in data]
        self._refresh_table()

    # ── Lookup table (record of past alignments) ──

    def _refresh_lookup_table(self):
        # setColumnCount() drops every width whenever the recorded key set
        # changes, so remember them by label and put them back afterwards.
        self._col_widths.update(self._current_col_widths())
        all_keys = list(dict.fromkeys(k for row in self._records for k in row))
        # ensure Timestamp is always first
        if "Timestamp" in all_keys:
            all_keys.remove("Timestamp")
            all_keys.insert(0, "Timestamp")
        self._lookup_table.blockSignals(True)
        self._lookup_table.setColumnCount(len(all_keys))
        self._lookup_table.setHorizontalHeaderLabels(all_keys)
        self._lookup_table.setRowCount(len(self._records))
        for r, row in enumerate(self._records):
            for c, lbl in enumerate(all_keys):
                item = QTableWidgetItem(str(row.get(lbl, "")))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._lookup_table.setItem(r, c, item)
        self._lookup_table.blockSignals(False)
        self._apply_col_widths()

    def _current_col_widths(self):
        """The lookup table's live {header label: width} map."""
        widths = {}
        for c in range(self._lookup_table.columnCount()):
            head = self._lookup_table.horizontalHeaderItem(c)
            if head is not None:
                widths[head.text()] = self._lookup_table.columnWidth(c)
        return widths

    def _apply_col_widths(self):
        """Re-apply remembered widths to whichever columns are on screen."""
        for c in range(self._lookup_table.columnCount()):
            head = self._lookup_table.horizontalHeaderItem(c)
            if head is None:
                continue
            width = self._col_widths.get(head.text())
            if width:
                self._lookup_table.setColumnWidth(c, width)

    def append_record_row(self, values: dict):
        self._records.append(dict(values))
        self._refresh_lookup_table()
        self.changed.emit()

    def _del_record_row(self):
        r = self._lookup_table.currentRow()
        if r < 0:
            return
        self._records.pop(r)
        self._refresh_lookup_table()
        self.changed.emit()

    def _clear_records(self):
        reply = QMessageBox.question(self, "Clear Lookup Table",
                                     "Remove all rows from the lookup table?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._records.clear()
            self._refresh_lookup_table()
            self.changed.emit()

    def _import_lookup_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Lookup CSV", "", "CSV files (*.csv)")
        if not path:
            return
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self._records.append(dict(row))
        self._refresh_lookup_table()
        self.changed.emit()

    def _export_lookup_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Lookup CSV", "lookup_table.csv", "CSV files (*.csv)")
        if not path:
            return
        all_keys = list(dict.fromkeys(k for row in self._records for k in row))
        if "Timestamp" in all_keys:
            all_keys.remove("Timestamp")
            all_keys.insert(0, "Timestamp")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            for row in self._records:
                writer.writerow({k: row.get(k, "") for k in all_keys})

    def get_record_data(self):
        return [dict(r) for r in self._records]

    def set_record_data(self, data):
        self._records = [dict(r) for r in data]
        self._refresh_lookup_table()

    def get_lookup_col_widths(self):
        """Lookup-table column widths by header label, for the saved config."""
        widths = dict(self._col_widths)
        widths.update(self._current_col_widths())
        return widths

    def set_lookup_col_widths(self, widths):
        """Restore saved widths.

        Labels with no column on screen yet are still remembered, so a width
        saved for a rarely recorded PV comes back when that PV next appears.
        """
        for label, width in dict(widths or {}).items():
            try:
                self._col_widths[str(label)] = int(width)
            except (TypeError, ValueError):
                continue      # a hand-edited config must not break start-up
        self._apply_col_widths()


# ─── Mirror stripe indicator ──────────────────────────────────────────────────
class MirrorStripeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        lbl = QLabel("Mirror Stripe:")
        lbl.setStyleSheet(f"color: {PAL['text_sec']}; font-size: 11px;")
        lay.addWidget(lbl)
        self._btns = {}
        for stripe in ("Si", "Rh", "Pt"):
            b = QLabel(stripe)
            b.setFixedSize(38, 22)
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b.setStyleSheet(self._off_style())
            self._btns[stripe] = b
            lay.addWidget(b)
        self._changing = QLabel("Changing…")
        self._changing.setStyleSheet(f"color: {PAL['amber']}; font-size: 11px; font-style: italic;")
        self._changing.setVisible(False)
        lay.addWidget(self._changing)
        lay.addStretch()

    def _off_style(self):
        return (f"background: {PAL['surface']}; border: 1px solid {PAL['border']};"
                f" border-radius: 3px; color: {PAL['text_dim']}; font-size: 11px;")

    def _on_style(self):
        return (f"background: {PAL['green']}; border: 1px solid {PAL['green']};"
                f" border-radius: 3px; color: white; font-size: 11px; font-weight: 700;")

    def set_stripe(self, stripe):
        if stripe == "changing":
            for b in self._btns.values():
                b.setStyleSheet(self._off_style())
            self._changing.setVisible(True)
        else:
            self._changing.setVisible(False)
            for name, b in self._btns.items():
                b.setStyleSheet(self._on_style() if name == stripe else self._off_style())

# ─── Motor → scan plot mapping ────────────────────────────────────────────────
# ─── Scan figure registry ───────────────────────────────────
# Everything about the plot area derives from these two tables: which tabs
# exist, which device owns them, the axis text, the legend text, the trace
# colours and the marker style. Adding a scan later is one row in each.
#
# (fig_id, device, tab text, plot title, x label, y label, fill under curve)
_FIGURE_DEFS = [
    ("dcm_pitch", "DCM",    "Pitch motor",  "DCM Pitch Motor",
     "DCM pitch (µrad)",        "Intensity (a.u.)", True),
    ("dcm_roll",  "DCM",    "Roll",         "DCM Roll Motor",
     "DCM roll (µrad)",         "BPM X (mm)",       False),
    ("dcm_piezo", "DCM",    "Pitch piezo",  "DCM Pitch Piezo",
     "DCM pitch piezo (DCOM)",  "Intensity (a.u.)", True),
    ("mir_slit",  "Mirror", "Slit",         "Slit Centre",
     "Slit centre (mm)",        "Signal (a.u.)",    True),
    ("mir_pitch", "Mirror", "Pitch motor",  "Mirror Pitch Motor",
     "Mirror pitch (µrad)",      "BPM Y (mm)",       False),
    ("mir_piezo", "Mirror", "Mirror piezo", "Mirror Pitch Piezo",
     "Mirror piezo (DCOM)",     "BPM Y (mm)",       False),
    ("mir_vdm",   "Mirror", "VDM:Y",        "VDM:Y Scan",
     "VDM:Y (µm)",              "Signal (a.u.)",    True),
    ("mir_vfm",   "Mirror", "VFM:Y",        "Coupled VFM:Y + VDM:Y",
     "VFM:Y (µm)",              "Signal (a.u.)",    True),
]
_PLOT_DEVICES = ("DCM", "Mirror")

# substep key (as emitted by scan_point/scan_peak/substep_status)
#   -> (fig_id, legend label, marker kind)
# Two figures carry two traces: the coarse/fine pitch pair and the mirror piezo
# before and after the feedback work. The piezo scans deliberately sit on their
# own figures because their x axis is a DCOM demand, not a motor position.
_SCAN_ROUTES = {
    "3_3b": ("dcm_pitch", "3B  pitch coarse",         "peak"),
    "3_3d": ("dcm_pitch", "3D  pitch fine",           "peak"),
    "3_3c": ("dcm_roll",  "3C  roll → BPM X = 0",     "zero"),
    "5_5b": ("dcm_piezo", "5B  DCM piezo",            "peak"),
    "4_4A": ("mir_slit",  "4A  slit centre",          "peak"),
    "4_4C": ("mir_pitch", "4C  mirror pitch motor",   "zero"),
    "5_5c": ("mir_piezo", "5C  mirror piezo (FB on)", "zero"),
    "4_4D": ("mir_vdm",   "4D  VDM:Y",                "peak"),
    "4_4E": ("mir_vfm",   "4E  VFM:Y + VDM:Y",        "peak"),
}


class ScanSeries:
    """One scan's points on one figure. Deliberately free of Qt so a future
    Alignment History tab can rebuild it from a saved run."""

    def __init__(self, key, label, color, marker_kind="peak"):
        self.key         = key
        self.label       = label
        self.color       = color
        self.marker_kind = marker_kind      # "peak" | "zero"
        self.xs, self.ys = [], []           # sorted by x, for drawing
        self.raw         = []               # (x, y) in acquisition order
        self.marker      = None

    def add_point(self, x, y):
        # The adaptive scans emit their extension and fine passes out of x
        # order; inserting in place keeps the drawn curve monotonic while `raw`
        # preserves the true acquisition sequence.
        i = bisect.bisect_left(self.xs, x)
        self.xs.insert(i, x)
        self.ys.insert(i, y)
        self.raw.append((x, y))

    def set_marker(self, value):
        self.marker = value

    def to_dict(self):
        return {"key": self.key, "label": self.label, "color": self.color,
                "marker_kind": self.marker_kind, "marker": self.marker,
                "xs": list(self.xs), "ys": list(self.ys), "raw": list(self.raw)}

    @classmethod
    def from_dict(cls, d):
        sr = cls(d["key"], d["label"], d["color"], d.get("marker_kind", "peak"))
        sr.xs, sr.ys = list(d.get("xs", [])), list(d.get("ys", []))
        sr.raw, sr.marker = list(d.get("raw", [])), d.get("marker")
        return sr


class FigureModel(QObject):
    """One logical figure: axis metadata plus its series, in creation order."""

    series_added   = pyqtSignal(str)
    point_added    = pyqtSignal(str)
    marker_changed = pyqtSignal(str)
    cleared        = pyqtSignal()

    def __init__(self, fig_id, device, tab_text, title,
                 x_label, y_label, fill=False, parent=None):
        super().__init__(parent)
        self.fig_id   = fig_id
        self.device   = device
        self.tab_text = tab_text
        self.title    = title
        self.x_label  = x_label
        self.y_label  = y_label
        self.fill     = fill
        self._series  = {}

    def order(self):
        return list(self._series.values())

    def get(self, key):
        return self._series.get(key)

    def is_empty(self):
        return not self._series

    def series(self, key, label, marker_kind="peak"):
        """Get or create a trace. Colour is assigned by insertion order, so a
        figure's first trace is always blue and its second always orange."""
        sr = self._series.get(key)
        if sr is None:
            color = SERIES_COLORS[len(self._series) % len(SERIES_COLORS)]
            sr = ScanSeries(key, label, color, marker_kind)
            self._series[key] = sr
            self.series_added.emit(key)
        return sr

    def add_point(self, key, label, x, y, marker_kind="peak"):
        self.series(key, label, marker_kind).add_point(x, y)
        self.point_added.emit(key)

    def set_marker(self, key, label, value, marker_kind="peak"):
        self.series(key, label, marker_kind).set_marker(value)
        self.marker_changed.emit(key)

    def clear(self):
        if self._series:
            self._series = {}
            self.cleared.emit()

    def to_dict(self):
        return {"fig_id": self.fig_id, "device": self.device, "title": self.title,
                "x_label": self.x_label, "y_label": self.y_label,
                "series": [sr.to_dict() for sr in self.order()]}


class ScanFigureView(QWidget):
    """One PlotWidget rendering one FigureModel.

    Several views may share a model: a QTabWidget cannot hold the same widget
    twice, so each pane owns its own view and they stay in step through the
    model's signals. That also lets the two panes keep independent zoom.
    """

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self._model  = model
        self._items  = {}          # series key -> (curve, scatter, marker line)
        self._dirty  = set()
        self._zero   = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._plot = make_plot(model.title, model.y_label, model.x_label)
        self._plot.setMinimumHeight(170)
        self._legend = self._plot.addLegend(offset=(-10, 10))
        lay.addWidget(self._plot)
        model.series_added.connect(self._redraw)
        model.point_added.connect(self._redraw)
        model.marker_changed.connect(self._apply_marker)
        model.cleared.connect(self.rebuild_from_model)
        self.refresh_theme()
        self.rebuild_from_model()

    # ── items ────────────────────────────────────────────────

    def _ensure_items(self, sr):
        if sr.key in self._items:
            return self._items[sr.key]
        pen = pg.mkPen(sr.color, width=2)
        if self._model.fill:
            curve = self._plot.plot([], [], pen=pen, name=sr.label,
                                    fillLevel=0, brush=pg.mkBrush(sr.color + "30"))
        else:
            curve = self._plot.plot([], [], pen=pen, name=sr.label)
        scatter = pg.ScatterPlotItem(size=5, brush=pg.mkBrush(sr.color), pen=None)
        self._plot.addItem(scatter)
        marker = pg.InfiniteLine(angle=90, pen=pg.mkPen(sr.color, width=1.5,
                                                        style=Qt.PenStyle.DashLine))
        marker.setVisible(False)
        self._plot.addItem(marker)
        if sr.marker_kind == "zero" and self._zero is None:
            self._zero = pg.InfiniteLine(
                angle=0, pos=0.0,
                pen=pg.mkPen(PAL["border"], style=Qt.PenStyle.DashLine))
            self._plot.addItem(self._zero)
        self._items[sr.key] = (curve, scatter, marker)
        return self._items[sr.key]

    def _draw(self, sr):
        curve, scatter, _m = self._ensure_items(sr)
        curve.setData(sr.xs, sr.ys)
        scatter.setData(sr.xs, sr.ys)

    def _redraw(self, key):
        sr = self._model.get(key)
        if sr is None:
            return
        if not self.isVisible():
            self._dirty.add(key)      # a hidden pane costs nothing during a scan
            return
        self._draw(sr)

    def _apply_marker(self, key):
        sr = self._model.get(key)
        if sr is None or sr.marker is None:
            return
        if not self.isVisible():
            self._dirty.add(key)
            return
        _c, _s, marker = self._ensure_items(sr)
        marker.setValue(sr.marker)
        marker.setVisible(True)

    def rebuild_from_model(self):
        self._plot.clear()
        self._items = {}
        self._zero  = None
        if self._legend is not None:
            self._legend.clear()
        for sr in self._model.order():
            self._draw(sr)
            if sr.marker is not None:
                _c, _s, marker = self._items[sr.key]
                marker.setValue(sr.marker)
                marker.setVisible(True)
        self._dirty.clear()

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            self._dirty.clear()
            self.rebuild_from_model()

    def refresh_theme(self):
        style_plot(self._plot, self._model.title, self._model.x_label,
                   self._model.y_label)
        lg = self._legend
        if lg is not None:
            for setter, value in (("setLabelTextColor", PAL["text_sec"]),
                                  ("setBrush", pg.mkBrush(PAL["surface"] + "cc")),
                                  ("setPen", pg.mkPen(PAL["border"]))):
                try:
                    getattr(lg, setter)(value)
                except Exception:
                    pass      # older pyqtgraph: degrade rather than raise
        if self._zero is not None:
            self._zero.setPen(pg.mkPen(PAL["border"], style=Qt.PenStyle.DashLine))


class FigurePane(QTabWidget):
    """One pane: a tab per figure of one device. Pages build their view lazily."""

    figure_selected = pyqtSignal(str)

    def __init__(self, device, models, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self._models = [m for m in models if m.device == device]
        self._order  = [m.fig_id for m in self._models]
        self._views  = {}
        for model in self._models:
            page = QWidget()
            page_lay = QVBoxLayout(page)
            page_lay.setContentsMargins(0, 0, 0, 0)
            self.addTab(page, model.tab_text)
        self.tabBar().setExpanding(False)
        self.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)
        self.currentChanged.connect(self._on_current_changed)
        self.refresh_theme()
        self._build_page(0)

    def _build_page(self, index):
        if not (0 <= index < len(self._order)):
            return
        fig_id = self._order[index]
        if fig_id in self._views:
            return
        view = ScanFigureView(self._models[index])
        self.widget(index).layout().addWidget(view)
        self._views[fig_id] = view

    def _on_current_changed(self, index):
        self._build_page(index)
        if 0 <= index < len(self._order):
            self.figure_selected.emit(self._order[index])

    def show_figure(self, fig_id):
        if fig_id in self._order:
            self.setCurrentIndex(self._order.index(fig_id))

    def current_fig(self):
        i = self.currentIndex()
        return self._order[i] if 0 <= i < len(self._order) else None

    def refresh_theme(self):
        # The global QSS tab rule (8px/20px, 12pt) is far too heavy for two
        # nested tab bars, so style this bar directly.
        self.tabBar().setStyleSheet(
            f"QTabBar::tab {{ background: {PAL['surface']}; color: {PAL['text_sec']};"
            f" padding: 3px 10px; font-size: 11px; font-weight: 600;"
            f" border: none; border-bottom: 2px solid transparent; }}"
            f"QTabBar::tab:selected {{ color: {PAL['cyan']};"
            f" border-bottom: 2px solid {PAL['cyan']}; }}")
        for view in self._views.values():
            view.refresh_theme()


class DeviceSplitView(QWidget):
    """Two panes over one device's figures: a live one and a browser.

    The left pane always shows whatever is scanning; the right is the
    operator's and the app never changes it. The captions say so, because the
    previous version expressed this through a checkbox in the tab-bar corner
    that nobody found.
    """

    @staticmethod
    def _caption(text, colour_key="text_dim"):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {PAL[colour_key]}; font-size: 10px;"
                          f" letter-spacing: 1px; padding: 0 2px;")
        return lbl

    def __init__(self, device, models, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.left  = FigurePane(device, models)
        self.right = FigurePane(device, models)
        for pane, text, key in ((self.left,  "LIVE SCAN", "cyan"),
                                (self.right, "BROWSE",    "text_dim")):
            box = QWidget()
            col = QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            col.addWidget(self._caption(text, key))
            col.addWidget(pane, 1)
            split.addWidget(box)
        split.setSizes([1, 1])
        lay.addWidget(split)
        if self.right.count() > 1:
            self.right.setCurrentIndex(1)

    def reset_selection(self):
        """Only the live pane resets; the browser stays where it was parked."""
        self.left.setCurrentIndex(0)

    def refresh_theme(self):
        self.left.refresh_theme()
        self.right.refresh_theme()


class ScanPlotBoard(QWidget):
    """The whole figure area, and the only plot object AlignmentTab touches."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._models = {}
        order = []
        for fig_id, device, tab_text, title, x_label, y_label, fill in _FIGURE_DEFS:
            model = FigureModel(fig_id, device, tab_text, title,
                                x_label, y_label, fill, self)
            self._models[fig_id] = model
            order.append(model)
        self._running  = False
        self._meta     = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._device_tabs = QTabWidget()
        self._device_tabs.setDocumentMode(True)
        self._devices = {}
        for device in _PLOT_DEVICES:
            view = DeviceSplitView(device, order)
            self._devices[device] = view
            self._device_tabs.addTab(view, device)
        lay.addWidget(self._device_tabs)
        self.setMinimumHeight(280)
        self.refresh_theme()

    # ── focus policy ─────────────────────────────────────────

    def on_substep(self, substep_key, status):
        route = _SCAN_ROUTES.get(substep_key)
        if not route or status != "running":
            return
        fig_id = route[0]
        device = self._models[fig_id].device
        panes  = self._devices[device]
        # Unconditional. This used to skip the left pane when the right pane
        # already showed the figure, which meant 3C and 4C moved nothing at all
        # and the scan appeared not to be plotted anywhere.
        self._device_tabs.setCurrentWidget(panes)
        panes.left.show_figure(fig_id)

    # ── data in ──────────────────────────────────────────────

    def add_point(self, substep_key, x, y):
        route = _SCAN_ROUTES.get(substep_key)
        if route:
            fig_id, label, kind = route
            self._models[fig_id].add_point(substep_key, label, x, y, kind)

    def set_marker(self, substep_key, value):
        route = _SCAN_ROUTES.get(substep_key)
        if route:
            fig_id, label, kind = route
            self._models[fig_id].set_marker(substep_key, label, value, kind)

    # ── run lifecycle ────────────────────────────────────────

    def begin_run(self, meta=None):
        self._meta = dict(meta or {})
        for model in self._models.values():
            model.clear()
        for view in self._devices.values():
            view.reset_selection()
        self._device_tabs.setCurrentIndex(0)
        self._running = True

    def end_run(self):
        self._running = False

    # ── misc ─────────────────────────────────────────────────

    def model(self, fig_id):
        return self._models.get(fig_id)

    def snapshot(self):
        """The whole run as plain data — the hook for a future history tab."""
        return {"meta": dict(self._meta),
                "figures": [self._models[f[0]].to_dict() for f in _FIGURE_DEFS]}

    def refresh_theme(self):
        for view in self._devices.values():
            view.refresh_theme()

# ─── Alignment Tab ────────────────────────────────────────────────────────────
class AlignmentTab(QWidget):
    alignment_done = pyqtSignal(bool)   # emitted after worker finishes; True = success
    run_requested  = pyqtSignal(object)  # set of substep keys, or None for "as ticked"

    # Identifies this app in the run lock file (see acquire_run_lock()) so a
    # refusal message and a stale-lock prompt can name which app holds it.
    _RUN_LOCK_APP_NAME = "DCM Alignment Console (full)"

    _SUBSTEP_TEXT = {
        "1_1a": "Read Energy table",
        "2_2a": "Turn off BPM feedback",
        "2_2b": "Apply Energy table settings",
        "2_2c": "Mirror out",
        "3_3a": "Puts DCM Piezo at 5",
        "3_3b": "Pitch scan → intensity peak",
        "3_3c": "Roll scan → BPM x = 0",
        "3_3d": "Pitch scan → intensity peak",
        "4_4A": "Slit scan → beam center",
        "4_4B": "Mirror in",
        "4_4B2": "Centre mirror piezo at 5",
        "4_4C": "Mirror pitch motor scan → BPM y = 0",
        "4_4D": "VDM:Y scan → peak",
        "4_4E": "Coupled VFM:Y+VDM:Y → peak",
        "4_4F": "Open mirror slits",
        "5_5mir": "Mirror in",
        "5_5jjc": "Close JJC slit before feedback",
        "5_5a": "Turn on H feedback",
        "5_5b": "DCM piezo pitch scan → max intensity",
        "5_5c": "Mirror piezo pitch scan → BPM y = 0",
        "5_5d": "Turn on V feedback",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_row     = None
        self._worker           = None
        self._thread           = None
        self._last_scan_results = {}
        self._running          = False
        self._faulted          = False
        self._fault_dlg        = None
        self._fault_rows       = []   # [(pv, context, reason, timestamp), ...]
        self._chapter_chk      = {}   # step number -> QCheckBox
        self._chapter_run_btn  = {}   # step number -> QPushButton
        self._substep_chk      = {}   # substep key  -> QCheckBox
        self._chapter_steps    = {}   # step number -> [substep key, ...]
        self._syncing_checks   = False
        self._stripe_future    = None
        self._stripe_pool      = None
        self._stripe_timer     = None
        self._holds_run_lock   = False  # True while this tab's run owns RUN_LOCK_PATH
        self._build()
        QTimer.singleShot(800, self._refresh_stripe_display)

    @staticmethod
    def _badge_style(color):
        return (f"border: 2px solid {color}; border-radius: 11px; color: {color};"
                f" font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 700;")

    def _build(self):
        main_lay = QHBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # ═══════════════════ LEFT PANEL ═════════════════════════════
        left = QWidget()
        left.setFixedWidth(372)
        left.setStyleSheet(
            f"background: {PAL['surface']}; border-right: 1px solid {PAL['border']};"
        )
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(12, 12, 12, 12)
        left_lay.setSpacing(8)

        # Controls
        ctrl_box = QGroupBox()
        ctrl_l = QVBoxLayout(ctrl_box)
        ctrl_l.setSpacing(6)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.start_btn = QPushButton("▶  Start Alignment")
        self.start_btn.setFixedWidth(160)
        self.abort_btn = QPushButton("■  Abort")
        self.abort_btn.setFixedWidth(80)
        self.abort_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.abort_btn)
        btn_row.addStretch()
        ctrl_l.addLayout(btn_row)
        self._refresh_start_btn()
        self._refresh_abort_btn()
        self.confirm_chk = QCheckBox("Confirm each step")
        self.confirm_chk.setChecked(False)
        self.confirm_chk.setToolTip(
            "When checked, alignment pauses after each scan substep for operator review"
        )
        ctrl_l.addWidget(self.confirm_chk)
        self.proceed_btn = QPushButton("▶  Proceed")
        self.proceed_btn.setFixedWidth(120)
        self.proceed_btn.setVisible(False)
        self.proceed_btn.clicked.connect(self._proceed_clicked)
        ctrl_l.addWidget(self.proceed_btn)
        # PV fault banner — hidden unless a PV fault is pending or active
        self.fault_banner = QWidget()
        self.fault_banner.setVisible(False)
        fb = QVBoxLayout(self.fault_banner)
        fb.setContentsMargins(8, 6, 8, 6)
        fb.setSpacing(3)
        self.fault_tag = QLabel()
        self.fault_tag.setWordWrap(True)
        self.fault_pv_lbl = QLabel()
        self.fault_pv_lbl.setWordWrap(True)
        self.fault_btn = styled_button("Review fault…")
        fb.addWidget(self.fault_tag)
        fb.addWidget(self.fault_pv_lbl)
        fb.addWidget(self.fault_btn)
        self.fault_btn.clicked.connect(self._open_fault_dialog)
        ctrl_l.addWidget(self.fault_banner)
        self.progress = QProgressBar()
        self.progress.setRange(0, 5)
        self.progress.setValue(0)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        ctrl_l.addWidget(self.progress)
        self.row_label = QLabel("No energy row selected — choose one in Energy Table")
        self.row_label.setStyleSheet(f"color: {PAL['amber']}; font-size: 10px;")
        self.row_label.setWordWrap(True)
        ctrl_l.addWidget(self.row_label)
        left_lay.addWidget(ctrl_box)

        # Beam path
        self.beam_path = BeamPathWidget()
        left_lay.addWidget(self.beam_path)

        # Step list (scrollable)
        step_scroll = QScrollArea()
        step_scroll.setWidgetResizable(True)
        step_scroll.setFrameShape(QFrame.Shape.NoFrame)
        step_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        steps_inner = QWidget()
        steps_lay = QVBoxLayout(steps_inner)
        steps_lay.setContentsMargins(0, 4, 0, 4)
        steps_lay.setSpacing(1)

        self._step_row_info = {}    # step_num → {"widget", "badge", "tag"}
        self._substep_labels = {}   # "step_substep" → QLabel

        step_defs = [
            (1, "Load Energy Table Settings",
             [("1a", "Read Energy table")]),
            (2, "Apply Energy Table Settings",
             [("2a", "Turn off BPM feedback"),
              ("2b", "Apply Energy table settings"),
              ("2c", "Mirror out")]),
            (3, "DCM Piezo Alignment",
             [("3a", "Puts DCM Piezo at 5"),
              ("3b", "Pitch scan → intensity peak"),
              ("3c", "Roll scan → BPM x = 0"),
              ("3d", "Pitch scan → intensity peak")]),
            (4, "Mirror Alignment",
             [("4A", "Slit scan → beam center"),
              ("4B", "Mirror in"),
              ("4B2", "Centre mirror piezo at 5"),
              ("4C", "Mirror pitch motor scan → BPM y = 0"),
              ("4D", "VDM:Y scan → peak"),
              ("4E", "Coupled VFM:Y+VDM:Y → peak"),
              ("4F", "Open mirror slits")]),
            (5, "Enable Feedback Loops",
             [("5mir", "Mirror in"),
              ("5jjc", "Close JJC slit before feedback"),
              ("5a", "Turn on H feedback"),
              ("5b", "DCM piezo pitch scan → max intensity"),
              ("5c", "Mirror piezo pitch scan → BPM y = 0"),
              ("5d", "Turn on V feedback")]),
        ]

        for step_num, title, substeps in step_defs:
            step_w = QWidget()
            sh = QHBoxLayout(step_w)
            sh.setContentsMargins(6, 5, 6, 5)
            sh.setSpacing(8)
            chk = QCheckBox()
            chk.setChecked(True)
            chk.setToolTip("Include this whole chapter in the run")
            chk.toggled.connect(lambda on, n=step_num: self._on_chapter_toggled(n, on))
            self._chapter_chk[step_num] = chk
            run_btn = QPushButton("\u25b6")
            run_btn.setFixedSize(22, 20)
            run_btn.setStyleSheet(
                f"QPushButton {{ padding: 0px; font-size: 9px;"
                f" color: {PAL['cyan']}; border: 1px solid {PAL['border']};"
                f" border-radius: 3px; background: {PAL['bg']}; }}"
                f"QPushButton:hover {{ border-color: {PAL['cyan']}; }}"
                f"QPushButton:disabled {{ color: {PAL['border']}; }}")
            run_btn.setToolTip(f"Run chapter {step_num} on its own")
            run_btn.clicked.connect(lambda _c=False, n=step_num: self._run_chapter(n))
            self._chapter_run_btn[step_num] = run_btn
            badge = QLabel(str(step_num))
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(self._badge_style(PAL["text_dim"]))
            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet(
                f"color: {PAL['text_sec']}; font-size: 11px; font-weight: 600;"
            )
            tag = make_tag("Idle", "grey")
            sh.addWidget(chk)
            sh.addWidget(badge)
            sh.addWidget(title_lbl, 1)
            sh.addWidget(run_btn)
            sh.addWidget(tag)
            self._step_row_info[step_num] = {"widget": step_w, "badge": badge, "tag": tag}
            steps_lay.addWidget(step_w)

            self._chapter_steps[step_num] = []
            for sub_id, sub_txt in substeps:
                key = f"{step_num}_{sub_id}"
                self._chapter_steps[step_num].append(key)
                # The row used to be a bare QLabel; it is now a container so a
                # checkbox can sit beside it. _substep_labels still points at
                # the label, so every existing status update is unaffected.
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(14, 0, 0, 0)
                rl.setSpacing(4)
                sub_chk = QCheckBox()
                sub_chk.setChecked(True)
                sub_chk.setToolTip("Include this step in the run")
                sub_chk.toggled.connect(lambda _on, k=key: self._on_substep_toggled(k))
                self._substep_chk[key] = sub_chk
                sub_lbl = QLabel(f"○  {sub_id.upper()}: {sub_txt}")
                sub_lbl.setStyleSheet(
                    f"color: {PAL['text_dim']}; font-size: 11px;"
                )
                self._substep_labels[key] = sub_lbl
                rl.addWidget(sub_chk)
                rl.addWidget(sub_lbl, 1)
                steps_lay.addWidget(row)

        steps_lay.addStretch()

        step_scroll.setWidget(steps_inner)
        left_lay.addWidget(step_scroll, 1)
        main_lay.addWidget(left)

        # ═══════════════════ RIGHT PANEL ════════════════════════════
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(12, 12, 12, 12)
        right_lay.setSpacing(8)

        # BPM strip
        bpm_w = QGroupBox("Live BPM")
        bpm_l = QHBoxLayout(bpm_w)
        bpm_l.setSpacing(16)
        for attr, label, unit in [
            ("_bpm_x_lbl", "BPM X", "mm"),
            ("_bpm_y_lbl", "BPM Y", "mm"),
            ("_bpm_i_lbl", "Intensity", "a.u."),
        ]:
            w, val = make_readout(label, "—", unit)
            setattr(self, attr, val)
            bpm_l.addWidget(w)
            bpm_l.addWidget(make_separator())
        fb_w = QWidget()
        fb_l = QVBoxLayout(fb_w)
        fb_l.setContentsMargins(0, 0, 0, 0)
        self._fb_h = make_tag("H FB  OFF", "grey")
        self._fb_v = make_tag("V FB  OFF", "grey")
        fb_l.addWidget(self._fb_h)
        fb_l.addWidget(self._fb_v)
        bpm_l.addWidget(fb_w)
        bpm_l.addStretch()
        right_lay.addWidget(bpm_w)

        self._stripe_widget = MirrorStripeWidget()
        right_lay.addWidget(self._stripe_widget)

        # Scan figures: device tabs, each with two independently tabbed panes
        self._plot_board = ScanPlotBoard()
        right_lay.addWidget(self._plot_board, 1)

        # Log
        log_box = QGroupBox("Alignment Log")
        log_l = QVBoxLayout(log_box)
        log_l.setContentsMargins(8, 8, 8, 8)
        self.log = LogWidget()
        self.log.setMaximumHeight(180)
        clr = styled_button("Clear")
        clr.setMaximumWidth(80)
        clr.clicked.connect(self.log.clear)
        log_l.addWidget(self.log)
        log_l.addWidget(clr)
        right_lay.addWidget(log_box)

        main_lay.addWidget(right, 1)
        self.start_btn.clicked.connect(lambda: self.run_requested.emit(None))
        self.abort_btn.clicked.connect(self.abort_alignment)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_tag(self, tag, text, color):
        tag.setText(text)
        obj = {"green": "tag_green", "amber": "tag_amber",
               "red": "tag_red", "cyan": "tag_cyan"}.get(color, "tag_grey")
        tag.setObjectName(obj)
        tag.style().unpolish(tag)
        tag.style().polish(tag)

    def _set_step_ui(self, step_num, status):
        info = self._step_row_info.get(step_num)
        if not info:
            return
        color = {"idle": PAL["text_dim"], "running": PAL["amber"],
                 "done": PAL["green"],    "error":   PAL["red"]}.get(status, PAL["text_dim"])
        info["badge"].setStyleSheet(self._badge_style(color))
        tag_map = {"idle": ("Idle", "grey"), "running": ("Running…", "amber"),
                   "done": ("Done", "green"), "error": ("Error", "red")}
        self._set_tag(info["tag"], *tag_map.get(status, ("Idle", "grey")))
        if status == "running":
            info["widget"].setStyleSheet(
                f"background: #fff8e7; border-radius: 4px;"
            )
        else:
            info["widget"].setStyleSheet("background: transparent; border-radius: 4px;")

    def _on_chapter_toggled(self, step_num, on):
        """A chapter box drives all of its substeps."""
        if self._syncing_checks:
            return
        self._syncing_checks = True
        try:
            for key in self._chapter_steps.get(step_num, []):
                self._substep_chk[key].setChecked(on)
        finally:
            self._syncing_checks = False

    def _on_substep_toggled(self, key):
        """Reflect a substep change back into its chapter box."""
        if self._syncing_checks:
            return
        step_num = int(key.split("_", 1)[0])
        keys = self._chapter_steps.get(step_num, [])
        n_on = sum(1 for k in keys if self._substep_chk[k].isChecked())
        self._syncing_checks = True
        try:
            box = self._chapter_chk[step_num]
            box.setTristate(0 < n_on < len(keys))
            if n_on == 0:
                box.setCheckState(Qt.CheckState.Unchecked)
            elif n_on == len(keys):
                box.setCheckState(Qt.CheckState.Checked)
            else:
                box.setCheckState(Qt.CheckState.PartiallyChecked)
        finally:
            self._syncing_checks = False

    def enabled_keys(self):
        """Substep keys the operator has left ticked."""
        return {k for k, c in self._substep_chk.items() if c.isChecked()}

    def set_chapter_enabled(self, step_num, on):
        self._chapter_chk[step_num].setChecked(bool(on))
        self._on_chapter_toggled(step_num, bool(on))

    def set_all_enabled(self, on=True):
        for step_num in self._chapter_chk:
            self.set_chapter_enabled(step_num, on)

    def _run_chapter(self, step_num):
        """Run one chapter on its own, leaving the tick boxes as they are.

        Chapter 1 rides along because it only logs the selected row, which is
        useful context at the top of the log.
        """
        keys = set(self._chapter_steps.get(step_num, []))
        keys |= set(self._chapter_steps.get(1, []))
        self.run_requested.emit(keys)

    def _refresh_stripe_display(self):
        """Read the mirror stripe position in the background and update the badge.

        Polled with a QTimer rather than `while not fut.done(): processEvents()`,
        which burned a whole core for up to 4 s at startup and re-entered the
        event loop from inside a slot.
        """
        if not EPICS_AVAILABLE or self._stripe_future is not None:
            return

        def _read():
            try:
                import epics as _epics
                try:
                    _epics.ca.use_initial_context()   # per-thread CA context
                except Exception:
                    pass
                vfm = _epics.caget(_STRIPE_VFM_X_PV + ".RBV", timeout=2.0, connection_timeout=2.0)
                vdm = _epics.caget(_STRIPE_VDM_X_PV + ".RBV", timeout=2.0, connection_timeout=2.0)
                if vfm is None or vdm is None:
                    return None
                for stripe, pos in _STRIPE_POSITIONS.items():
                    if abs(vfm - pos["vfm_x"]) <= 10 and abs(vdm - pos["vdm_x"]) <= 10:
                        return stripe
            except Exception:
                pass
            return None

        self._stripe_pool   = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._stripe_future = self._stripe_pool.submit(_read)
        self._stripe_timer  = QTimer(self)
        self._stripe_timer.setInterval(150)
        self._stripe_timer.timeout.connect(self._poll_stripe)
        self._stripe_timer.start()

    def _poll_stripe(self):
        if self._stripe_future is None or not self._stripe_future.done():
            return
        self._stripe_timer.stop()
        try:
            stripe = self._stripe_future.result()
        except Exception:
            stripe = None
        self._stripe_future = None
        try:
            self._stripe_pool.shutdown(wait=False)
        except Exception:
            pass
        if stripe:
            self._stripe_widget.set_stripe(stripe)

    def _set_substep(self, key, status, detail=""):
        lbl = self._substep_labels.get(key)
        if not lbl:
            return
        sub_id = (key.split("_", 1)[1] if "_" in key else key).upper()
        base = f"{sub_id}: {self._SUBSTEP_TEXT.get(key, key)}"
        suffix = f" ({detail})" if detail else ""
        if status == "skipped":
            lbl.setText(f"⊘  {base}")
            lbl.setStyleSheet(
                f"color: {PAL['text_dim']}; font-size: 11px; font-style: italic;")
        elif status == "running":
            lbl.setText(f"⟳  {base}")
            lbl.setStyleSheet(
                f"color: {PAL['amber']}; font-size: 11px; font-weight: 600;"
            )
        elif status == "waiting":
            lbl.setText(f"⏸  {base}{suffix}")
            lbl.setStyleSheet(
                f"color: {PAL['amber']}; font-size: 11px; font-style: italic;"
            )
        elif status == "done":
            lbl.setText(f"✓  {base}{suffix}")
            lbl.setStyleSheet(f"color: {PAL['green']}; font-size: 11px;")
        else:
            lbl.setText(f"○  {base}")
            lbl.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 11px;")

    # ── Public ────────────────────────────────────────────────────────────────

    def _refresh_abort_btn(self):
        self.abort_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {PAL['bg']};"
            f"  color: {PAL['red']};"
            f"  border: 2px solid {PAL['red']};"
            f"  border-radius: 5px;"
            f"  padding: 6px 16px;"
            f"  font-weight: 600;"
            f"  font-size: 12px;"
            f"}}"
            f"QPushButton:hover {{ background: {PAL['tag_red_bg']}; }}"
            f"QPushButton:disabled {{"
            f"  background: {PAL['surface']};"
            f"  color: {PAL['border']};"
            f"  border-color: {PAL['surface_hi']};"
            f"}}"
        )

    def _refresh_start_btn(self):
        self.start_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {PAL['cyan_dim']};"
            f"  color: {PAL['text_pri']};"
            f"  border: 2px solid {PAL['cyan']};"
            f"  border-radius: 5px;"
            f"  padding: 7px 20px;"
            f"  font-size: 13px;"
            f"  font-weight: 700;"
            f"}}"
            f"QPushButton:hover {{ background: {PAL['cyan']}; color: {PAL['text_pri']}; }}"
            f"QPushButton:disabled {{"
            f"  background: {PAL['surface_hi']};"
            f"  color: {PAL['text_dim']};"
            f"  border-color: {PAL['border']};"
            f"}}"
        )

    def set_selected_row(self, row):
        self._selected_row = row
        self.row_label.setText(
            f"MonoE={row['mono_e']} keV  UE={row['ue']} keV  "
            f"Roll={row['roll']}  Pitch={row['pitch']}"
        )
        self.row_label.setStyleSheet(
            f"color: {PAL['cyan']}; font-size: 10px;"
            f" font-family: 'JetBrains Mono', monospace;"
        )

    def _set_selection_enabled(self, on):
        """Lock the tick boxes and chapter-run buttons while a run is going."""
        for box in self._chapter_chk.values():
            box.setEnabled(on)
        for box in self._substep_chk.values():
            box.setEnabled(on)
        for btn in self._chapter_run_btn.values():
            btn.setEnabled(on)

    def _acquire_run_lock_or_refuse(self):
        """Claim RUN_LOCK_PATH for this run, or tell the operator why not.

        Both this console and the mini console write feedback_v/auto_feedback
        and drive the mirror stages -- starting one mid-run of the other would
        have them fight over the same PVs. Called right before start_alignment
        does anything else, so a refusal leaves the UI exactly as it was.

        A live lock (holder pid still running) is a hard refusal: only that
        operator can clear it. A stale lock (holder pid no longer running,
        almost always a crash) is offered as an override rather than silently
        taken -- acquire_run_lock() itself would overwrite it unconditionally,
        but asking first means an operator who is unsure never does it by
        accident.
        """
        existing = read_run_lock()
        if existing is not None and not existing.get("stale"):
            QMessageBox.warning(
                self, "Alignment already running",
                "A DCM alignment is already running in %s\n"
                "on host %s (pid %s), started %s.\n\n"
                "Wait for it to finish, or ask that operator to abort it, "
                "before starting another run here." % (
                    existing.get("app") or "another instance",
                    existing.get("host") or "an unknown host",
                    existing.get("pid", "?"),
                    existing.get("started") or "an unknown time"))
            return False
        if existing is not None and existing.get("stale"):
            reply = QMessageBox.question(
                self, "Stale run lock",
                "A run lock recorded for %s\n"
                "on host %s (pid %s, started %s) is present, but that "
                "process is no longer running -- most likely it crashed or "
                "was killed without cleaning up.\n\n"
                "Override the stale lock and start this run?" % (
                    existing.get("app") or "another instance",
                    existing.get("host") or "an unknown host",
                    existing.get("pid", "?"),
                    existing.get("started") or "an unknown time"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return False
        ok, holder = acquire_run_lock(self._RUN_LOCK_APP_NAME)
        if not ok:
            # Lost a race with another process between the check above and
            # the acquire itself.
            holder = holder or {}
            QMessageBox.warning(
                self, "Alignment already running",
                "Could not claim the run lock -- %s just started a run.\n"
                "Wait for it to finish before starting another run here."
                % (holder.get("app") or "another instance"))
            return False
        self._holds_run_lock = True
        return True

    def start_alignment(self, pvs=None, scan_params=None, simulate=True,
                        mirror_stages=None, enabled=None):
        if self._running:
            QMessageBox.information(self, "Alignment already running",
                                    "A sequence is already in progress. Abort it first.")
            return
        if not self._selected_row:
            QMessageBox.warning(self, "No row selected",
                                "Please select an energy row in the Energy Table tab first.")
            return
        pvs           = pvs          or DEFAULT_PVS
        scan_params   = scan_params  or DEFAULT_SCAN
        mirror_stages = mirror_stages or DEFAULT_MIRROR_STAGES
        if enabled is None:
            enabled = self.enabled_keys()
        enabled = set(enabled)
        if not enabled:
            QMessageBox.warning(self, "Nothing to run",
                                "Every step is unticked, so there is nothing to do.")
            return
        # Chapter 4 is "skipped" exactly when none of its steps is enabled.
        skip_mirror = not any(k.startswith("4_") for k in enabled)

        if not self._acquire_run_lock_or_refuse():
            return

        self._reset_ui()
        self._plot_board.begin_run({
            "row": dict(self._selected_row),
            "started": datetime.now().isoformat(timespec="seconds"),
            "skip_mirror": skip_mirror,
            "enabled": sorted(enabled),
            "simulate": simulate,
        })
        self._running = True
        self.start_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        self._set_selection_enabled(False)
        self.confirm_chk.setEnabled(False)
        # Only the chapters that will actually run count toward the bar.
        self.progress.setRange(0, max(len({k.split("_", 1)[0] for k in enabled}), 1))

        self._thread = QThread()
        self._worker = AlignmentWorker(
            pvs, scan_params, self._selected_row,
            simulate=simulate,
            skip_mirror=skip_mirror,
            enabled=enabled,
            mirror_stages=mirror_stages,
            confirm_mode=self.confirm_chk.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_signal.connect(self._on_log)
        self._worker.step_status.connect(self._on_step_status)
        self._worker.scan_point.connect(self._on_scan_point)
        self._worker.scan_peak.connect(self._on_scan_peak)
        self._worker.bpm_update.connect(self._on_bpm_update)
        self._worker.feedback_update.connect(self._on_feedback)
        self._worker.substep_status.connect(self._on_substep_status)
        self._worker.confirm_needed.connect(self._on_confirm_needed)
        self._worker.scan_results_ready.connect(self._on_scan_results)
        self._worker.stripe_status.connect(self._stripe_widget.set_stripe)
        self._worker.pv_fault.connect(self._on_pv_fault)
        self._worker.pv_fault_cleared.connect(self._on_pv_fault_cleared)
        self._worker.paused_changed.connect(self._on_paused_changed)
        self._worker.preflight_report.connect(self._on_preflight_report)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def abort_alignment(self):
        if self._worker:
            self._worker.abort()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_log(self, msg, level):
        self.log.append_log(msg, level)

    def _on_step_status(self, step, status):
        self._set_step_ui(step, status)
        self.beam_path.update_step(step, status)
        done = sum(
            1 for info in self._step_row_info.values()
            if info["tag"].text() == "Done"
        )
        self.progress.setValue(done)

    def _on_scan_point(self, substep_key, x, y):
        self._plot_board.add_point(substep_key, x, y)

    def _on_scan_peak(self, substep_key, peak):
        self._plot_board.set_marker(substep_key, peak)

    def _on_substep_status(self, key, status):
        """Fan out one worker signal so the ordering is deterministic."""
        self._set_substep(key, status)
        self._plot_board.on_substep(key, status)

    def refresh_plot_theme(self):
        self._plot_board.refresh_theme()

    def _on_bpm_update(self, x, y, intensity):
        self._bpm_x_lbl.setText(f"{x:+.4f}")
        self._bpm_y_lbl.setText(f"{y:+.4f}")
        self._bpm_i_lbl.setText(f"{intensity:.3f}")
        _s = "font-family: 'JetBrains Mono', monospace; font-size: 18px; font-weight: 700;"
        self._bpm_x_lbl.setStyleSheet(
            f"color: {PAL['green'] if abs(x) < 0.005 else PAL['amber']}; {_s}"
        )
        self._bpm_y_lbl.setStyleSheet(
            f"color: {PAL['green'] if abs(y) < 0.005 else PAL['amber']}; {_s}"
        )
        self._bpm_i_lbl.setStyleSheet(
            f"color: {PAL['green'] if intensity > 0.9 else PAL['cyan']}; {_s}"
        )

    def _on_feedback(self, h, v):
        self._set_tag(self._fb_h, f"H FB  {'ON' if h else 'OFF'}", "green" if h else "grey")
        self._set_tag(self._fb_v, f"V FB  {'ON' if v else 'OFF'}", "green" if v else "grey")

    def _on_scan_results(self, results: dict):
        self._last_scan_results = results

    def _on_confirm_needed(self, substep_key):
        self.proceed_btn.setVisible(True)
        self.proceed_btn.setEnabled(not self._faulted)
        self.log.append_log(f"  ⏸  Waiting for operator confirmation after {substep_key} — click Proceed to continue", "warn")

    def _proceed_clicked(self):
        if self._faulted or self._fault_dlg is not None:
            return   # resolve the PV fault first
        self.proceed_btn.setEnabled(False)
        self.proceed_btn.setVisible(False)
        if self._worker:
            self._worker.confirm()

    # ── PV fault UI ───────────────────────────────────────────

    def _set_fault_banner(self, state, pv=""):
        """state: "" hidden | "pending" amber | "paused" red.

        The amber state is honest about latency: a CA monitor has reported a
        disconnect but the worker has not reached a safe point yet, so something
        may still be moving. Red means the worker is genuinely blocked.
        """
        if not state:
            self.fault_banner.setVisible(False)
            return
        paused = (state == "paused")
        col = PAL["red"] if paused else PAL["amber"]
        bg  = PAL["tag_red_bg"] if paused else PAL["tag_amber_bg"]
        self.fault_banner.setStyleSheet(
            f"background: {bg}; border: 1px solid {col}; border-radius: 4px;")
        self.fault_tag.setText("PAUSED — PV FAULT" if paused
                               else "FAULT DETECTED — pausing at next safe point…")
        self.fault_tag.setStyleSheet(f"color: {col}; font-size: 11px; font-weight: 700;")
        self.fault_pv_lbl.setText(pv)
        self.fault_pv_lbl.setVisible(bool(pv))
        self.fault_pv_lbl.setStyleSheet(
            f"color: {col}; font-size: 10px; font-family: 'JetBrains Mono', monospace;")
        self.fault_btn.setVisible(paused)
        self.fault_banner.setVisible(True)

    def show_pending_fault(self, pv_name):
        """A live CA monitor saw a PV drop; the worker has not stopped yet."""
        if self._running and not self._faulted:
            self._set_fault_banner("pending", pv_name)

    def _on_pv_fault(self, pv, context, reason):
        self._faulted = True
        self._fault_rows.append(
            (pv, context, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.proceed_btn.setEnabled(False)
        self._set_fault_banner("paused", pv)
        if self._fault_dlg is not None:
            # Never stack a second dialog — refresh the open one in place.
            self._fault_dlg.refresh(self._fault_rows)
            self._fault_dlg.set_busy(False)
            return
        self._open_fault_dialog()
        QApplication.beep()

    def _open_fault_dialog(self):
        if self._fault_dlg is not None:
            self._fault_dlg.raise_()
            self._fault_dlg.activateWindow()
            return
        if not self._fault_rows:
            return
        dlg = PVFaultDialog(self._fault_rows, self.window())
        dlg.retry_clicked.connect(self._fault_retry)
        dlg.abort_clicked.connect(self._fault_abort)
        dlg.finished.connect(self._on_fault_dlg_finished)
        self._fault_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_fault_dlg_finished(self, _result):
        # Dismissing the dialog does not resume anything: the worker is still
        # blocked on its event. The banner keeps a way back in.
        self._fault_dlg = None

    def _fault_retry(self):
        if self._worker:
            self._worker.fault_retry()

    def _fault_abort(self):
        if self._worker:
            self._worker.fault_abort()

    def _on_pv_fault_cleared(self, _action):
        self._clear_fault_ui()

    def _on_paused_changed(self, paused):
        if not paused:
            self._set_fault_banner("")

    def _clear_fault_ui(self):
        self._faulted = False
        self._fault_rows = []
        if self._fault_dlg is not None:
            dlg, self._fault_dlg = self._fault_dlg, None
            dlg.close()
        self._set_fault_banner("")

    def _on_preflight_report(self, rows):
        for label, pv, status in rows:
            level = "ok" if status.startswith("ok") else (
                "info" if status == "skipped" else "error")
            self.log.append_log(f"    {label:<26} {pv or '(none)':<38} {status}", level)

    def _on_finished(self, success):
        self._running = False
        if self._holds_run_lock:
            release_run_lock()
            self._holds_run_lock = False
        self._plot_board.end_run()
        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        self.proceed_btn.setVisible(False)
        self._set_selection_enabled(True)
        self.confirm_chk.setEnabled(True)
        self._clear_fault_ui()
        if not success:
            # Otherwise the step that was interrupted sits on "Running…" forever.
            for step_num, info in self._step_row_info.items():
                if info["tag"].text().startswith("Running"):
                    self._set_step_ui(step_num, "error")
                    self.beam_path.update_step(step_num, "error")
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self.alignment_done.emit(success)

    def _reset_ui(self):
        self._clear_fault_ui()
        for step_num in self._step_row_info:
            self._set_step_ui(step_num, "idle")
        for step in range(1, 6):
            self.beam_path.update_step(step, "idle")
        self.progress.setValue(0)
        for key in self._substep_labels:
            self._set_substep(key, "idle")
        self.log.clear()


# ─── Mirror Tab ───────────────────────────────────────────────────────────────
class MirrorTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mirror_stages = [dict(s) for s in DEFAULT_MIRROR_STAGES]
        self._scan_fields   = {}
        self._is_simulate   = lambda: False
        self._build()

    def set_simulate_fn(self, fn):
        self._is_simulate = fn

    def _build(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        # ── Mirror Alignment Parameters ──
        scan_box = QGroupBox("Mirror Alignment Parameters")
        scan_lay = QGridLayout(scan_box)
        scan_lay.setSpacing(8)
        scan_lay.addWidget(QLabel("Signal source"), 0, 0)
        sig_cb = QComboBox()
        sig_cb.addItems(["BPM Intensity", "Ion Chamber"])
        self._scan_fields["mir_signal"] = sig_cb
        scan_lay.addWidget(sig_cb, 0, 1)
        sig_cb.currentTextChanged.connect(self.changed)

        mir_scan_defs = [
            ("mir_slit_size_a",    "Slit size 4A (mm)",     QDoubleSpinBox,  0.01,  5.0,   0.1,  3),
            ("mir_slit_cen_start", "Slit scan start (mm)",  QDoubleSpinBox, -20.0,  0.0,  -2.0,  2),
            ("mir_slit_cen_stop",  "Slit scan stop (mm)",   QDoubleSpinBox,  0.0,  20.0,   2.0,  2),
            ("mir_slit_cen_steps", "Slit scan steps",       QSpinBox,        3,    200,    21,   0),
            ("mir_slit_size_b",    "Slit size 4C (mm)",     QDoubleSpinBox,  0.01,  5.0,   0.2,  3),
            ("mir_vdm_start",      "VDM scan start offset", QDoubleSpinBox, -5000., 0.,  -500.,  1),
            ("mir_vdm_stop",       "VDM scan stop offset",  QDoubleSpinBox,  0., 5000.,   500.,  1),
            ("mir_vdm_steps",      "VDM scan steps",        QSpinBox,        3,    200,    21,   0),
            ("mir_vfm_start",      "VFM scan start offset", QDoubleSpinBox, -5000., 0.,  -250.,  1),
            ("mir_vfm_stop",       "VFM scan stop offset",  QDoubleSpinBox,  0., 5000.,   250.,  1),
            ("mir_vfm_steps",      "VFM scan steps",        QSpinBox,        3,    200,    21,   0),
            ("mir_slit_size_c",    "Slit size 4F (mm)",       QDoubleSpinBox, 0.01, 20.0,  1.0,  3),
            ("jjc_size_pre_feedback", "JJC size before feedback (mm)", QDoubleSpinBox, 0.0, 20.0, 0.4, 3),
            ("mir_pitch_start",    "Pitch motor scan start (µrad)", QDoubleSpinBox, -5000., 0.,  -50., 1),
            ("mir_pitch_stop",     "Pitch motor scan stop (µrad)",  QDoubleSpinBox,  0., 5000.,   50., 1),
            ("mir_pitch_steps",    "Pitch motor scan steps",        QSpinBox,        3,   200,    21,  0),
        ]
        for r, (key, lbl, cls, mn, mx, dflt, dec) in enumerate(mir_scan_defs, start=1):
            scan_lay.addWidget(QLabel(lbl), r, 0)
            if cls == QDoubleSpinBox:
                sb = NoScrollDoubleSpinBox()
                sb.setDecimals(dec)
                sb.setRange(mn, mx)
                sb.setValue(dflt)
            else:
                sb = NoScrollSpinBox()
                sb.setRange(int(mn), int(mx))
                sb.setValue(int(dflt))
            sb.setMinimumWidth(100)
            self._scan_fields[key] = sb
            scan_lay.addWidget(sb, r, 1)
            sb.valueChanged.connect(self.changed)
        top_row.addWidget(scan_box, 1)

        # ── Procedure summary ──
        proc_box = QGroupBox("Procedure Overview")
        proc_l = QVBoxLayout(proc_box)
        proc_lbl = QLabel(
            "<b>4A</b> Slit scan (mirror out): scan slit center → signal peak → center slit.<br>"
            "<b>4B</b> Mirror in: insert all mirror stages into beam path.<br>"
            "<b>4B2</b> Centre the mirror pitch piezo at 5 (mid-range).<br>"
            "<b>4C</b> Mirror pitch scan: narrow slit → scan the mirror pitch motor → BPM y = 0.<br>"
            "<b>4D</b> VDM:Y scan: scan VDM:Y → signal peak → move.<br>"
            "<b>4E</b> Coupled VFM+VDM scan: scan VFM:Y with VDM step = 2× VFM step → move both to peak.<br>"
            "<b>4F</b> Open the mirror slits back to their working vertical extent."
        )
        proc_lbl.setWordWrap(True)
        proc_lbl.setTextFormat(Qt.TextFormat.RichText)
        proc_lbl.setStyleSheet(f"color: {PAL['text_sec']}; font-size: 12px; padding: 4px;")
        proc_l.addWidget(proc_lbl)
        top_row.addWidget(proc_box, 1)
        lay.addLayout(top_row)

        # ── Mirror Stages table ──
        stages_box = QGroupBox("Mirror Stages — In / Out Positions")
        stages_l = QVBoxLayout(stages_box)
        btn_bar = QHBoxLayout()
        add_btn  = styled_button("+ Add Stage")
        del_btn  = styled_button("Remove Stage")
        test_btn = styled_button("Test Stage PVs")
        for b in [add_btn, del_btn, test_btn]:
            btn_bar.addWidget(b)
        btn_bar.addStretch()
        stages_l.addLayout(btn_bar)

        self._mirror_table = QTableWidget()
        self._mirror_table.setColumnCount(4)
        self._mirror_table.setHorizontalHeaderLabels(["Name", "PV", "Value (In)", "Value (Out)"])
        # PV names are long and the value columns are short, so the operator
        # needs to be able to redistribute the width by hand.
        self._mirror_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._mirror_table.horizontalHeader().setStretchLastSection(True)
        self._mirror_table.setAlternatingRowColors(True)
        self._mirror_table.setRowCount(len(self._mirror_stages))
        for r, stage in enumerate(self._mirror_stages):
            for c, key in enumerate(["name", "pv", "val_in", "val_out"]):
                self._mirror_table.setItem(r, c, QTableWidgetItem(str(stage[key])))
        self._mirror_table.cellChanged.connect(self._on_mirror_cell_changed)
        stages_l.addWidget(self._mirror_table)
        lay.addWidget(stages_box, 1)

        add_btn.clicked.connect(self._add_stage)
        del_btn.clicked.connect(self._del_stage)
        test_btn.clicked.connect(self._test_stage_pvs)

        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_mirror_cell_changed(self, r, c):
        if r >= len(self._mirror_stages):
            return
        key = ["name", "pv", "val_in", "val_out"][c]
        item = self._mirror_table.item(r, c)
        if item is None:
            return
        text = item.text()
        if key in ("val_in", "val_out"):
            try:
                self._mirror_stages[r][key] = float(text)
            except ValueError:
                return
        else:
            self._mirror_stages[r][key] = text
        self.changed.emit()

    def _add_stage(self):
        self._mirror_stages.append(
            {"name": "New Stage", "pv": "", "val_in": 0.0, "val_out": 0.0}
        )
        self._mirror_table.blockSignals(True)
        self._mirror_table.setRowCount(len(self._mirror_stages))
        r = len(self._mirror_stages) - 1
        for c, key in enumerate(["name", "pv", "val_in", "val_out"]):
            self._mirror_table.setItem(r, c, QTableWidgetItem(str(self._mirror_stages[r][key])))
        self._mirror_table.blockSignals(False)
        self.changed.emit()

    def _del_stage(self):
        r = self._mirror_table.currentRow()
        if r < 0:
            return
        self._mirror_stages.pop(r)
        self._mirror_table.blockSignals(True)
        self._mirror_table.removeRow(r)
        self._mirror_table.blockSignals(False)
        self.changed.emit()

    def _test_stage_pvs(self):
        simulate = self._is_simulate()
        stages   = list(self._mirror_stages)
        results  = [None] * len(stages)

        def test_one(stage):
            name, pv = stage["name"], stage["pv"].strip()
            if not pv:
                return (name, "—", "— (no PV)")
            if simulate:
                return (name, pv, "sim")
            if not EPICS_AVAILABLE:
                return (name, pv, "no EPICS")
            try:
                import epics as _epics
                val = _epics.caget(pv, timeout=2.0, connection_timeout=2.0)
                if val is None:
                    return (name, pv, "✗  timeout / not found")
                return (name, pv, f"✓  {val}")
            except Exception as exc:
                return (name, pv, f"✗  {exc}")

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = max(len(stages), 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                future_map = {ex.submit(test_one, s): i for i, s in enumerate(stages)}
                pending    = set(future_map)
                while pending:
                    done, pending = concurrent.futures.wait(pending, timeout=0.05)
                    QApplication.processEvents()
                    for f in done:
                        results[future_map[f]] = f.result()
        finally:
            QApplication.restoreOverrideCursor()

        dlg = QDialog(self)
        dlg.setWindowTitle("Mirror Stage PV Test")
        dlg.resize(700, 380)
        lay = QVBoxLayout(dlg)

        tbl = QTableWidget(len(results), 3)
        tbl.setHorizontalHeaderLabels(["Stage Name", "PV Name", "Current Value / Status"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setAlternatingRowColors(True)

        ok_color  = QColor("#2e7d32")
        err_color = QColor("#b71c1c")
        dim_color = QColor("#888888")

        for r, (name, pv, status) in enumerate(results):
            tbl.setItem(r, 0, QTableWidgetItem(name))
            tbl.setItem(r, 1, QTableWidgetItem(pv))
            st_item = QTableWidgetItem(status)
            if status.startswith("✓"):
                st_item.setForeground(ok_color)
            elif status.startswith("✗"):
                st_item.setForeground(err_color)
            else:
                st_item.setForeground(dim_color)
            tbl.setItem(r, 2, st_item)

        lay.addWidget(tbl)
        btn = QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def get_mirror_stages(self):
        return [dict(s) for s in self._mirror_stages]

    def get_mirror_scan_params(self):
        out = {}
        for k, w in self._scan_fields.items():
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                out[k] = w.value()
            elif isinstance(w, QComboBox):
                out[k] = w.currentText()
        return out

    def apply_config(self, cfg):
        """Apply a saved config. Returns the keys that could not be applied."""
        bad = []
        if "mirror_stages" in cfg:
            # val_in/val_out are written straight to motors, so they must be
            # numbers even if the JSON carried strings.
            self._mirror_stages = [
                {**dict(st),
                 "name":    str(st.get("name", "")),
                 "pv":      str(st.get("pv", "")),
                 "val_in":  coerce_float(st.get("val_in", 0.0)),
                 "val_out": coerce_float(st.get("val_out", 0.0))}
                for st in cfg["mirror_stages"]
            ]
            self._mirror_table.blockSignals(True)
            self._mirror_table.setRowCount(len(self._mirror_stages))
            for r, stage in enumerate(self._mirror_stages):
                for c, key in enumerate(["name", "pv", "val_in", "val_out"]):
                    self._mirror_table.setItem(r, c, QTableWidgetItem(str(stage[key])))
            self._mirror_table.blockSignals(False)
        for k, v in cfg.get("mirror_scan", {}).items():
            if k not in self._scan_fields:
                continue
            w = self._scan_fields[k]
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                if not set_spin_value(w, v):
                    bad.append(k)
            elif isinstance(w, QComboBox):
                idx = w.findText(str(v))
                if idx >= 0:
                    w.setCurrentIndex(idx)
                else:
                    bad.append(k)
        return bad


# ─── Main Window ─────────────────────────────────────────────────────────────
class RecordTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pv_config   = [dict(r) for r in DEFAULT_RECORD_PVS]
        self._is_simulate = lambda: False
        self._build()

    def set_simulate_fn(self, fn):
        self._is_simulate = fn

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # ── PV config ──
        pv_box = QGroupBox("PVs to Record")
        pv_lay = QVBoxLayout(pv_box)

        pv_bar = QHBoxLayout()
        add_pv_btn  = styled_button("+ Add PV")
        del_pv_btn  = styled_button("Remove PV")
        test_pv_btn = styled_button("Test PVs")
        all_btn     = styled_button("Include All")
        pv_bar.addWidget(add_pv_btn)
        pv_bar.addWidget(del_pv_btn)
        pv_bar.addWidget(test_pv_btn)
        pv_bar.addWidget(all_btn)
        pv_bar.addStretch()
        pv_bar.addWidget(QLabel("☑ = include in next save   |   timestamp is always recorded"))
        pv_lay.addLayout(pv_bar)

        self._pv_table = QTableWidget()
        self._pv_table.setColumnCount(3)
        self._pv_table.setHorizontalHeaderLabels(["Include", "Label", "PV Name"])
        self._pv_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._pv_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._pv_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._pv_table.setAlternatingRowColors(True)
        pv_lay.addWidget(self._pv_table)
        lay.addWidget(pv_box, 1)

        add_pv_btn.clicked.connect(self._add_pv)
        del_pv_btn.clicked.connect(self._del_pv)
        test_pv_btn.clicked.connect(self._test_pvs)
        all_btn.clicked.connect(self._include_all)
        self._pv_table.cellChanged.connect(self._on_pv_cell_changed)
        self._pv_table.itemChanged.connect(self._on_pv_item_changed)

        self._refresh_pv_table()

    def _refresh_pv_table(self):
        self._pv_table.blockSignals(True)
        self._pv_table.setRowCount(len(self._pv_config))
        for r, entry in enumerate(self._pv_config):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if entry["checked"] else Qt.CheckState.Unchecked)
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._pv_table.setItem(r, 0, chk)
            lbl_item = QTableWidgetItem(entry["label"])
            if entry.get("locked", False):
                lbl_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                lbl_item.setToolTip("Label is locked to preserve CSV compatibility")
            self._pv_table.setItem(r, 1, lbl_item)
            self._pv_table.setItem(r, 2, QTableWidgetItem(entry["pv"]))
        self._pv_table.blockSignals(False)

    def _on_pv_item_changed(self, item):
        if item.column() != 0:
            return
        r = item.row()
        if r >= len(self._pv_config):
            return
        self._pv_config[r]["checked"] = (item.checkState() == Qt.CheckState.Checked)
        self.changed.emit()

    def _on_pv_cell_changed(self, r, c):
        if c == 0 or r >= len(self._pv_config):
            return
        item = self._pv_table.item(r, c)
        if item is None:
            return
        text = item.text()
        if c == 1:
            if self._pv_config[r].get("locked", False):
                return
            self._pv_config[r]["label"] = text
        elif c == 2:
            self._pv_config[r]["pv"] = text
        self.changed.emit()

    def _include_all(self):
        for entry in self._pv_config:
            entry["checked"] = True
        self._refresh_pv_table()
        self.changed.emit()

    def _add_pv(self):
        self._pv_config.append({"label": "New PV", "pv": "", "checked": False, "locked": False})
        self._refresh_pv_table()
        self.changed.emit()

    def _del_pv(self):
        r = self._pv_table.currentRow()
        if r < 0:
            return
        self._pv_config.pop(r)
        self._refresh_pv_table()
        self.changed.emit()

    def _test_pvs(self):
        simulate = self._is_simulate()
        entries  = list(self._pv_config)
        results  = [None] * len(entries)

        def test_one(entry):
            label, pv = entry["label"], entry["pv"]
            if entry.get("source") == "scan_result":
                if label == "Mirror Stripe":
                    if simulate:
                        return (label, "—", "sim")
                    if not EPICS_AVAILABLE:
                        return (label, "—", "no EPICS")
                    try:
                        import epics as _epics
                        vfm = _epics.caget(_STRIPE_VFM_X_PV + ".RBV", timeout=2.0, connection_timeout=2.0)
                        vdm = _epics.caget(_STRIPE_VDM_X_PV + ".RBV", timeout=2.0, connection_timeout=2.0)
                        if vfm is None or vdm is None:
                            return (label, "—", "✗  timeout")
                        for s, pos in _STRIPE_POSITIONS.items():
                            if abs(vfm - pos["vfm_x"]) <= 10 and abs(vdm - pos["vdm_x"]) <= 10:
                                return (label, "—", f"✓  {s}")
                        return (label, "—", f"✗  unknown  (VFM:X={vfm:.0f}  VDM:X={vdm:.0f})")
                    except Exception as exc:
                        return (label, "—", f"✗  {exc}")
                return (label, "—", "— (computed after alignment)")
            if not pv:
                return (label, "—", "— (no PV)")
            if simulate:
                return (label, pv, "sim")
            if not EPICS_AVAILABLE:
                return (label, pv, "no EPICS")
            _STRING_LABELS = {"BPM Sensitivity", "MonP Sensitivity Unit", "MonP Sensitivity Num"}
            try:
                import epics as _epics
                if label in _STRING_LABELS:
                    val = _epics.caget(pv, as_string=True, timeout=2.0, connection_timeout=2.0)
                    if val is None:
                        return (label, pv, "✗  timeout / not found")
                    return (label, pv, f"✓  {val}")
                val = _epics.caget(pv, timeout=2.0, connection_timeout=2.0)
                if val is None:
                    return (label, pv, "✗  timeout / not found")
                if label == "XTAL":
                    return (label, pv, f"✓  {fmt_xtal(val)}")
                return (label, pv, f"✓  {val}")
            except Exception as exc:
                return (label, pv, f"✗  {exc}")

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = max(len(entries), 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                future_map = {ex.submit(test_one, e): i for i, e in enumerate(entries)}
                pending    = set(future_map)
                while pending:
                    done, pending = concurrent.futures.wait(pending, timeout=0.05)
                    QApplication.processEvents()
                    for f in done:
                        results[future_map[f]] = f.result()
        finally:
            QApplication.restoreOverrideCursor()

        dlg = QDialog(self)
        dlg.setWindowTitle("PV Connection Test")
        dlg.resize(700, 400)
        lay = QVBoxLayout(dlg)

        tbl = QTableWidget(len(results), 3)
        tbl.setHorizontalHeaderLabels(["Label", "PV Name", "Status"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setAlternatingRowColors(True)

        ok_color  = QColor("#2e7d32")
        err_color = QColor("#b71c1c")
        dim_color = QColor("#888888")

        for r, (label, pv, status) in enumerate(results):
            tbl.setItem(r, 0, QTableWidgetItem(label))
            tbl.setItem(r, 1, QTableWidgetItem(pv))
            st_item = QTableWidgetItem(status)
            if status.startswith("✓"):
                st_item.setForeground(ok_color)
            elif status.startswith("✗"):
                st_item.setForeground(err_color)
            else:
                st_item.setForeground(dim_color)
            tbl.setItem(r, 2, st_item)

        lay.addWidget(tbl)
        btn = QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def get_checked_pvs(self):
        # "source" must be carried through: _on_alignment_done uses it to tell
        # computed scan results apart from real PVs. Dropping it meant the six
        # scan_result rows were never written to the lookup table.
        return [{"label": e["label"], "pv": e["pv"], "source": e.get("source", "")}
                for e in self._pv_config if e["checked"]]

    def get_pv_config(self):
        return [dict(r) for r in self._pv_config]

    def set_pv_config(self, config):
        self._pv_config = [{**r, "locked": r.get("locked", True)}
                           for r in config if r.get("label") != "Timestamp"]
        self._refresh_pv_table()


class MirrorSetupTab(QWidget):
    """Everything mirror-related in one tab.

    Composed from the shared PV panel and the existing mirror parameter and
    stage editor rather than merging them, so neither has to be rewritten.
    """

    def __init__(self, pv_panel, mirror_tab, parent=None):
        super().__init__(parent)
        self.pv_panel   = pv_panel
        self.mirror_tab = mirror_tab
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(pv_panel)
        split.addWidget(mirror_tab)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)


class MainWindow(QMainWindow):
    _bpm_polled       = pyqtSignal(float, float, float)
    _bpm_disconnected = pyqtSignal(str, str)   # (key, pv_name)

    _BPM_KEYS = ("bpm_x", "bpm_y", "bpm_intensity")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DCM Alignment Console")
        self.resize(1280, 860)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(QSS)

        central = QWidget()
        self.setCentralWidget(central)
        main_lay = QVBoxLayout(central)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # ── Top bar ──
        topbar = QWidget()
        topbar.setStyleSheet(f"background: {PAL['surface']}; border-bottom: 1px solid {PAL['border']};")
        topbar.setFixedHeight(56)
        tb_lay = QHBoxLayout(topbar)
        tb_lay.setContentsMargins(20, 0, 20, 0)

        title = QLabel()
        title.setText(
            f'<span style="color:{PAL["cyan"]}; font-family: JetBrains Mono, Consolas, monospace; font-size: 16px; font-weight: 700;">DCM</span>'
            f'<span style="color:{PAL["text_pri"]}; font-size: 15px; font-weight: 400;"> Alignment Console</span>'
        )
        sub = QLabel("Double Crystal Monochromator")
        sub.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 10px; letter-spacing: 2px; text-transform: uppercase;")

        title_w = QVBoxLayout()
        title_w.setSpacing(1)
        title_w.addWidget(title)
        title_w.addWidget(sub)
        tb_lay.addLayout(title_w)
        tb_lay.addStretch()

        epics_tag = make_tag("EPICS available" if EPICS_AVAILABLE else "Simulation mode", "green" if EPICS_AVAILABLE else "amber")
        tb_lay.addWidget(epics_tag)

        theme_lbl = QLabel("Theme:")
        theme_lbl.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 11px;")
        self._theme_lbl = theme_lbl
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(list(THEMES.keys()))
        self._theme_combo.setFixedWidth(130)
        self._theme_combo.currentTextChanged.connect(self._apply_theme)
        tb_lay.addSpacing(12)
        tb_lay.addWidget(theme_lbl)
        tb_lay.addWidget(self._theme_combo)

        main_lay.addWidget(topbar)

        # ── Tabs ──
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        main_lay.addWidget(self.tabs, 1)

        # Three instances of the same panel, filtered by section. setup_tab
        # stays as an alias for the global one so the many is_simulate() and
        # sim_check references keep working.
        self.global_tab = SetupTab(section="global", show_connection=True)
        self.dcm_tab    = SetupTab(section="dcm",    show_connection=False)
        self.mir_pv_tab = SetupTab(section="mirror", show_connection=False)
        self.setup_tab  = self.global_tab
        self._pv_panels = (self.global_tab, self.dcm_tab, self.mir_pv_tab)
        for panel in (self.dcm_tab, self.mir_pv_tab):
            panel.set_simulate_source(self.global_tab.is_simulate)
        self.global_tab.set_shared_hooks(all_pvs_fn=self.get_pvs,
                                         save_fn=self._save_config_as,
                                         load_fn=self._load_config_from)

        self.energy_tab    = EnergyTableTab()
        self.alignment_tab = AlignmentTab()
        self.mirror_tab    = MirrorTab()
        self.record_tab    = RecordTab()
        self.mirror_setup_tab = MirrorSetupTab(self.mir_pv_tab, self.mirror_tab)
        self.record_tab.set_simulate_fn(self.is_simulate)
        self.mirror_tab.set_simulate_fn(self.is_simulate)

        self.tabs.addTab(self.energy_tab,        "  Energy Table  ")
        self.tabs.addTab(self.global_tab,        "  PV Monitor / Config  ")
        self.tabs.addTab(self.dcm_tab,           "  DCM Setup  ")
        self.tabs.addTab(self.mirror_setup_tab,  "  Mirror Setup  ")
        self.tabs.addTab(self.alignment_tab,     "  Alignment  ")
        self.tabs.addTab(self.record_tab,        "  Record  ")

        # Wire up row selection → alignment tab
        self.energy_tab.row_selected.connect(self.alignment_tab.set_selected_row)

        # The Alignment tab asks for a run; it does not know where the config
        # lives. Previously MainWindow disconnected the button's own slot, which
        # dropped every other connection to it.
        self.alignment_tab.run_requested.connect(self._start_alignment)

        # ── Status bar ──
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

        self._auto_load_config()

        # `changed` fires on every keystroke and every spinbox tick, so debounce
        # rather than rewriting the whole config file each time.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_config)
        for tab in self._pv_panels + (self.energy_tab, self.mirror_tab,
                                      self.record_tab):
            tab.changed.connect(self._schedule_save)
        self.alignment_tab.alignment_done.connect(self._on_alignment_done)

        # Continuous BPM monitor via EPICS CA subscriptions
        self._bpm_monitored = {}  # "bpm_x"/"bpm_y"/"bpm_intensity" → epics.PV
        self._bpm_vals      = {"bpm_x": 0.0, "bpm_y": 0.0, "bpm_intensity": 0.0}
        self._bpm_names     = {}  # last-subscribed names, to avoid pointless churn
        self._resubscribing = set()
        self._bpm_polled.connect(self.alignment_tab._on_bpm_update)
        self.global_tab.sim_check.toggled.connect(self._on_sim_toggled_bpm)
        # The other panels follow the global Simulation checkbox.
        for panel in (self.dcm_tab, self.mir_pv_tab):
            self.global_tab.sim_check.toggled.connect(panel._on_sim_toggled)
        # Re-subscribe when a BPM PV name actually changes
        for panel in self._pv_panels:
            panel.changed.connect(self._refresh_bpm_monitors)
        # Route live-monitor disconnects into the alignment fault handler
        self._bpm_disconnected.connect(self._on_monitor_disconnect)
        for panel in self._pv_panels:
            panel.pv_disconnected.connect(self._on_monitor_disconnect)
        if EPICS_AVAILABLE and not self.is_simulate():
            self._start_bpm_monitoring()

    def _on_alignment_done(self, success):
        if not success:
            return
        reply = QMessageBox.question(
            self, "Save to Lookup Table",
            "Alignment completed successfully.\nSave results to the lookup table?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        checked      = self.record_tab.get_checked_pvs()
        simulate     = self.is_simulate()
        scan_results = self.alignment_tab._last_scan_results
        _STRING_LABELS = {"BPM Sensitivity", "MonP Sensitivity Unit", "MonP Sensitivity Num"}
        row = {"Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        for entry in checked:
            label  = entry["label"]
            pv     = entry["pv"]
            source = entry.get("source", "")
            if source == "scan_result":
                row[label] = scan_results.get(label, "—")
                continue
            if not pv:
                continue
            if simulate:
                row[label] = "sim"
            else:
                try:
                    import epics as _epics
                    if label in _STRING_LABELS:
                        val = _epics.caget(pv, as_string=True, timeout=2.0, connection_timeout=2.0)
                        row[label] = "—" if val is None else str(val)
                    elif label == "XTAL":
                        row[label] = fmt_xtal(_epics.caget(pv, timeout=2.0, connection_timeout=2.0))
                    else:
                        row[label] = fmt_pv_value(_epics.caget(pv, timeout=2.0, connection_timeout=2.0))
                except Exception:
                    row[label] = "err"
        self.energy_tab.append_record_row(row)
        self.tabs.setCurrentWidget(self.energy_tab)

    def _apply_theme(self, theme_name: str):
        if theme_name not in THEMES:
            return
        PAL.update(THEMES[theme_name])
        QApplication.instance().setStyleSheet(build_qss(PAL))
        self.alignment_tab._refresh_start_btn()
        self.alignment_tab._refresh_abort_btn()
        self.alignment_tab.refresh_plot_theme()
        # Refresh readback label styles on next PV update (they read PAL live)
        self._theme_lbl.setStyleSheet(f"color: {PAL['text_dim']}; font-size: 11px;")

    # ── merged views over the three settings panels ─────────────────────

    def get_pvs(self):
        """Every PV name, merged across the panels.

        cfg["pvs"] deliberately stays one flat dict so an existing
        dcm_config.json keeps loading without migration.
        """
        merged = {}
        for panel in self._pv_panels:
            merged.update(panel.get_pvs())
        return merged

    def get_scan_params(self):
        """Every scan parameter, merged across the panels."""
        merged = {}
        for panel in self._pv_panels:
            merged.update(panel.get_scan_params())
        return merged

    def is_simulate(self):
        return self.global_tab.is_simulate()

    def set_pv(self, key, text):
        """Set a PV name wherever it lives. Returns True if the key exists."""
        for panel in self._pv_panels:
            if key in panel._pv_fields:
                panel._pv_fields[key].setText(str(text))
                return True
        return False

    def set_scan_param(self, key, value):
        """Set a scan parameter wherever it lives. Returns True if it exists."""
        for panel in self._pv_panels:
            widget = panel._scan_fields.get(key)
            if widget is None:
                continue
            if isinstance(widget, QComboBox):
                idx = widget.findText(str(value))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            else:
                set_spin_value(widget, value)
            return True
        return False

    def _config_dict(self):
        return {
            "pvs": self.get_pvs(),
            "scan": self.get_scan_params(),
            "simulate": self.is_simulate(),
            "energy_table": self.energy_tab.get_table_data(),
            "mirror_stages": self.mirror_tab.get_mirror_stages(),
            "mirror_scan": self.mirror_tab.get_mirror_scan_params(),
            "record_pv_config": self.record_tab.get_pv_config(),
            "record_data": self.energy_tab.get_record_data(),
            "lookup_col_widths": self.energy_tab.get_lookup_col_widths(),
            "theme": self._theme_combo.currentText(),
        }

    def _apply_config_dict(self, cfg):
        """Apply a whole config. Returns the keys that could not be applied.

        Each panel picks out only the keys it owns and ignores the rest, which
        is what makes the flat schema survive the tab split.
        """
        bad = []
        for panel in self._pv_panels:
            bad += list(panel._apply_config(cfg) or [])
        if "energy_table" in cfg:
            self.energy_tab.set_table_data(cfg["energy_table"])
        bad += list(self.mirror_tab.apply_config(cfg) or [])
        if "record_pv_config" in cfg:
            self.record_tab.set_pv_config(cfg["record_pv_config"])
        if "record_data" in cfg:
            self.energy_tab.set_record_data(cfg["record_data"])
        # after set_record_data: the columns only exist once the rows do
        if "lookup_col_widths" in cfg:
            self.energy_tab.set_lookup_col_widths(cfg["lookup_col_widths"])
        if "theme" in cfg:
            idx = self._theme_combo.findText(cfg["theme"])
            if idx >= 0:
                self._theme_combo.setCurrentIndex(idx)
        return bad

    def _save_config_as(self):
        """The panel's "Save Config..." button. Writes the WHOLE config.

        It used to write only pvs/scan/simulate while defaulting the filename
        to dcm_config.json, so accepting that name destroyed the energy table,
        mirror stages, record config and recorded data.
        """
        path, _ = QFileDialog.getSaveFileName(self, "Save Config",
                                              "dcm_config.json", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(self._config_dict(), f, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, "Save Config", f"Could not write file:\n{exc}")

    def _load_config_from(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "",
                                              "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Load Config", f"Could not read file:\n{exc}")
            return
        bad = self._apply_config_dict(cfg)
        self.status.showMessage(
            f"Config loaded from {path}"
            + (f" — could not apply: {', '.join(sorted(set(bad)))}" if bad else ""))

    def _auto_load_config(self):
        if not os.path.exists(AUTO_CONFIG_PATH):
            return
        try:
            with open(AUTO_CONFIG_PATH) as f:
                cfg = json.load(f)
            bad = self._apply_config_dict(cfg)
            if bad:
                self.status.showMessage(
                    f"Config restored from {AUTO_CONFIG_PATH} — "
                    f"could not apply: {', '.join(sorted(set(bad)))}")
            else:
                self.status.showMessage(f"Config restored from {AUTO_CONFIG_PATH}")
        except Exception as e:
            self.status.showMessage(f"Could not restore config: {e}")

    def _save_config(self):
        """Write the auto-save config without destroying keys this app does not own.

        Re-reads the file immediately before writing -- not a snapshot taken
        at load time, which could restore a stale section over one a second
        process (the mini console) wrote more recently -- and carries over
        every top-level key _config_dict() does not produce. The write itself
        goes through atomic_write_json(), so a crash or a concurrent writer
        can no longer leave truncated JSON that _auto_load_config() cannot
        parse on the next launch.
        """
        cfg = self._config_dict()
        own_keys = set(cfg)
        existing = load_config_file(AUTO_CONFIG_PATH)
        for key, value in existing.items():
            if key not in own_keys:
                cfg[key] = value
        atomic_write_json(AUTO_CONFIG_PATH, cfg)

    def _on_sim_toggled_bpm(self, is_sim: bool):
        if is_sim or not EPICS_AVAILABLE:
            self._stop_bpm_monitoring()
        else:
            self._start_bpm_monitoring()

    def _mark_resubscribing(self, pv_name):
        """Suppress the disconnect callback caused by our own teardown."""
        name = (pv_name or "").strip()
        if not name:
            return
        self._resubscribing.add(name)
        QTimer.singleShot(3000, lambda n=name: self._resubscribing.discard(n))

    def _start_bpm_monitoring(self):
        import epics as _epics
        pvs = self.get_pvs()
        for key in self._BPM_KEYS:
            old = self._bpm_monitored.pop(key, None)
            if old is not None:
                try:
                    self._mark_resubscribing(getattr(old, "pvname", ""))
                    old.disconnect()
                except Exception:
                    pass
            pv_name = (pvs.get(key, "") or "").strip()
            self._bpm_names[key] = pv_name
            if not pv_name:
                continue

            def _cb(value=None, pvname=None, bk=key, **_kw):
                # libca callback thread — read a plain bool, never a Qt widget.
                if self.alignment_tab._running:
                    return   # the worker owns the BPM display during a run
                if value is None:
                    return
                try:
                    self._bpm_vals[bk] = float(value)
                except Exception:
                    return
                self._bpm_polled.emit(
                    self._bpm_vals["bpm_x"],
                    self._bpm_vals["bpm_y"],
                    self._bpm_vals["bpm_intensity"],
                )

            def _conn(pvname=None, conn=None, bk=key, nm=pv_name, **_kw):
                if not conn:
                    self._bpm_disconnected.emit(bk, nm)

            try:
                pv = _epics.PV(pv_name, callback=_cb, connection_callback=_conn,
                               auto_monitor=True)
                self._bpm_monitored[key] = pv
            except Exception:
                pass

    def _stop_bpm_monitoring(self):
        for pv in self._bpm_monitored.values():
            try:
                self._mark_resubscribing(getattr(pv, "pvname", ""))
                pv.disconnect()
            except Exception:
                pass
        self._bpm_monitored.clear()
        self._bpm_names.clear()

    def _refresh_bpm_monitors(self):
        """Re-subscribe only when a BPM PV name actually changed.

        `setup_tab.changed` also fires for scan-parameter spinboxes. Rebuilding
        the CA connections on every one of those produced a stream of spurious
        disconnect callbacks, which the fault router would take seriously.
        """
        if not (EPICS_AVAILABLE and not self.is_simulate()):
            return
        pvs = self.get_pvs()
        names = {k: (pvs.get(k, "") or "").strip() for k in self._BPM_KEYS}
        if names == self._bpm_names:
            return
        self._start_bpm_monitoring()

    def _schedule_save(self):
        self._save_timer.start()

    # ── live-monitor disconnect routing ───────────────────────────────

    def _active_worker(self):
        """The worker of a hardware run that is actually in progress, else None.

        Judged by the mode the run *started* with, not the live checkbox: the
        operator can flip Simulation mid-run, and that must not change how an
        in-flight hardware run is policed.
        """
        worker = self.alignment_tab._worker
        if worker is None or not self.alignment_tab._running or worker.simulate:
            return None
        return worker

    def _is_self_inflicted(self, pv_name):
        return (pv_name in self._resubscribing
                or any(pv_name in p._resubscribing for p in self._pv_panels))

    def _on_monitor_disconnect(self, key, pv_name):
        if self._active_worker() is None:
            # Idle: surface it, but do not interrupt anything.
            self.status.showMessage(f"PV disconnected: {pv_name}", 8000)
            return
        if self._is_self_inflicted(pv_name):
            return
        # Absorb IOC reboots and gateway blips before stopping a 20-minute run.
        QTimer.singleShot(2000, lambda: self._confirm_disconnect(key, pv_name))

    def _confirm_disconnect(self, key, pv_name):
        worker = self._active_worker()
        if worker is None or self._is_self_inflicted(pv_name):
            return
        pv = self._bpm_monitored.get(key)
        for panel in self._pv_panels:
            pv = pv or panel._monitored_pvs.get(key)
        if pv is not None and getattr(pv, "connected", False):
            return   # it came back on its own
        try:
            active = worker.active_pv_set()
        except Exception:
            active = set()
        if active and pv_name not in active and pv_name.split(".")[0] not in active:
            return   # this run never touches that PV
        if worker._paused or worker._pause_requested:
            return   # already faulting
        self.alignment_tab.show_pending_fault(pv_name)
        worker.external_fault(pv_name, f"live PV monitor ({key})",
                              "channel access connection lost during the run")

    def closeEvent(self, event):
        if self.alignment_tab._running and self.alignment_tab._worker is not None:
            reply = QMessageBox.question(
                self, "Alignment in progress",
                "An alignment sequence is still running.\n"
                "Abort it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Otherwise the worker thread keeps driving motors after the window
            # has gone.
            self.alignment_tab.abort_alignment()
            thread = self.alignment_tab._thread
            if thread is not None:
                thread.quit()
                thread.wait(5000)
        self._stop_bpm_monitoring()
        for panel in self._pv_panels:
            panel._stop_monitoring()
        # Belt-and-braces: _on_finished() releases the lock too, but that is a
        # queued cross-thread signal and may not have been delivered yet by
        # the time we get here (e.g. an abort-on-close above). release_run_lock()
        # is a no-op unless this process still owns the lock, so calling it
        # again here is always safe.
        release_run_lock()
        self.alignment_tab._holds_run_lock = False
        self._save_config()
        super().closeEvent(event)

    def _start_alignment(self, enabled=None):
        pvs = self.get_pvs()
        params = self.get_scan_params()
        params.update(self.mirror_tab.get_mirror_scan_params())
        simulate = self.is_simulate()
        mirror_stages = self.mirror_tab.get_mirror_stages()
        self.alignment_tab.start_alignment(pvs=pvs, scan_params=params, simulate=simulate,
                                           mirror_stages=mirror_stages, enabled=enabled)
        self.tabs.setCurrentWidget(self.alignment_tab)
        self.status.showMessage("Alignment running…")
        self.alignment_tab._worker.finished.connect(
            lambda ok: self.status.showMessage("Alignment complete ✓" if ok else "Alignment aborted")
        )
        self.alignment_tab._worker.paused_changed.connect(
            lambda paused: self.status.showMessage(
                "PAUSED — PV fault: respond in the fault dialog" if paused
                else "Alignment running…")
        )


# ─── Entry point ─────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DCM Alignment Console")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
