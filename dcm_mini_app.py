"""
DCM Mini Alignment — a smaller PyQt6 GUI for beamlines with no DCM piezo.

Context (see the design doc this file implements): the full `dcm_align_app.py`
console assumes a DCM pitch/roll piezo pair exists (chapter 3 centres it at 5,
and its pre-flight demands both piezo PVs). Some beamlines have no DCM piezo at
all, so this file runs a reduced, four-step sequence instead:

    1. Turn vertical feedback off (there is no piezo for it to drive).
    2. DCM pitch **motor** scan for maximum intensity on the **ion chamber**.
    3. If |BPM x| exceeds a threshold, scan the DCM roll **motor** to zero it;
       otherwise skip, and say so.
    4. Mirror pitch **piezo** scan to BPM y = 0 — pre-positioning only. V
       feedback is deliberately left OFF: with no DCM piezo there is no
       actuator for that loop to drive.

`piezo_pitch` / `piezo_roll` (the DCM piezos) are never read, written, or
probed anywhere in this file — that is the whole point of the tool.

This module is imported as `import dcm_align_app as app` and reaches every
module-level name through `app.<name>` at call time (never `from ... import`),
because `tests/_harness.py` rebinds `app.AUTO_CONFIG_PATH` and
`app.EPICS_AVAILABLE` for test isolation and a snapshot import would defeat
that.

Run:
    python dcm_mini_app.py
"""

import random
import sys
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QCheckBox, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt

import dcm_align_app as app


# ─── Mini-only scan parameters ────────────────────────────────────────────────
# Layered on top of app.DEFAULT_SCAN (never replacing it): _smart_scan_peak
# indexes self.params["settle_time"] directly rather than .get()-ing it, so the
# worker needs the whole shared scan dict present, not just these additions.
#
# smart_max_extend_steps overrides an EXISTING app.DEFAULT_SCAN key (not a
# mini_-prefixed one) because _smart_scan_peak reads that name directly; the
# design note behind this file explicitly calls for lowering it here so a
# flat/dead detector cannot extend the adaptive scan ten times before the
# contrast guard below gets a chance to refuse.
MINI_DEFAULT_SCAN = {
    "mini_pitch_half_width":        0.05,
    "mini_pitch_steps":             25,
    "mini_pitch_max_excursion":     0.5,
    "mini_min_contrast":            0.05,
    "mini_bpm_x_threshold_um":      12.0,
    "mini_roll_start":              -0.05,
    "mini_roll_step":               0.005,
    "mini_roll_max_excursion":      0.5,
    "mini_piezo_start":             -1.0,
    "mini_piezo_step":              0.1,
    "mini_piezo_max_excursion":     3.0,
    "mini_mirror_out_fraction":     0.5,
    "mini_repeak_pitch_after_roll": False,
    "smart_max_extend_steps":       4,
}

# substep key -> (label used in required-PV / record bookkeeping)
_MINI_STEP_TEXT = {
    "m_pitch":  "Pitch scan (motor) → intensity peak",
    "m_roll":   "Roll scan (motor) → BPM x = 0 (skipped if already within threshold)",
    "m_piezo":  "Mirror piezo pitch scan → BPM y = 0 (pre-positioning only)",
    "m_pitch2": "Re-peak pitch after roll (3D-equivalent) — default off",
}
_MINI_STEP_ORDER = ["m_pitch", "m_roll", "m_piezo", "m_pitch2"]

# One figure per axis. Deliberately independent of app._FIGURE_DEFS /
# app._PLOT_DEVICES / app._SCAN_ROUTES: those module tables are asserted
# elsewhere to carry exactly the main board's 8 figures, and FigurePane /
# ScanFigureView accept any device string and know nothing about boards, so a
# second, unrelated table here changes nothing about the main app.
_MINI_FIGURE_DEFS = [
    ("mini_pitch", "Mini", "Pitch", "DCM Pitch Motor",
     "DCM pitch (urad)",       "Ion chamber (a.u.)", True),
    ("mini_roll",  "Mini", "Roll",  "DCM Roll Motor",
     "DCM roll (urad)",        "BPM X (um)",         False),
    ("mini_piezo", "Mini", "Piezo", "Mirror Pitch Piezo",
     "Mirror piezo (DCOM)",    "BPM Y (um)",         False),
]

# substep key -> (fig_id, legend label, marker kind)
_MINI_SCAN_ROUTES = {
    "m_pitch":  ("mini_pitch", "Pitch scan",                     "peak"),
    "m_pitch2": ("mini_pitch", "Pitch re-peak (3D-equivalent)",  "peak"),
    "m_roll":   ("mini_roll",  "Roll → BPM X = 0",          "zero"),
    "m_piezo":  ("mini_piezo", "Mirror piezo → BPM Y = 0",  "zero"),
}


def _fmt(v):
    """Small formatting helper for the run-history table. Never raises."""
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return "%.4g" % v
    return str(v)


# ─── Worker ────────────────────────────────────────────────────────────────────
class MiniAlignmentWorker(app.AlignmentWorker):
    """The reduced, no-DCM-piezo sequence, built on the shared worker machinery.

    Reused unchanged from app.AlignmentWorker: _read, _read_float, _wait_motor_done,
    _motor_deadband, _verify_arrival, _pv_is_motor, _fault, _fault_many,
    _pause_point, external_fault, _preflight, _smart_scan_peak, _scan_to_zero,
    _pre_scan, _require_beam, _require_feedback_off, _signal_pv, abort,
    confirm/request_confirm, fault_retry/fault_abort.

    Overridden: _required_pvs (a much smaller set, and never the DCM piezos),
    _write (adds the travel-window refusal), _run_sequence (the four-step
    sequence itself, replacing the five-chapter one).
    """

    # Emitted once per run, in the `finally` that also reports the V-feedback
    # state, so the window can persist a run record even on a failed run.
    record_ready = pyqtSignal(dict)

    def __init__(self, pvs, scan_params, mirror_stages=None, simulate=True,
                 enabled=None):
        full_params = dict(app.DEFAULT_SCAN)
        full_params.update(MINI_DEFAULT_SCAN)
        full_params.update(scan_params or {})
        # This is what redirects _smart_scan_peak's signal source: it resolves
        # its own PV via self._signal_pv("dcm_signal"), which reads this key.
        full_params["dcm_signal"] = "Ion Chamber"
        # smart_max_extend_steps is the one MINI_DEFAULT_SCAN key that shares
        # a name with an app.DEFAULT_SCAN key, and it is a safety choice, not
        # an operator setting (there is no mini_smart_max_extend_steps in the
        # config schema). Reassert it last so a caller that hands in a plain
        # copy of app.DEFAULT_SCAN or the full console's saved "scan" section
        # -- both of which legitimately carry the value 10 -- cannot silently
        # undo the mini's lower, safer extension budget for a flat detector.
        full_params["smart_max_extend_steps"] = MINI_DEFAULT_SCAN["smart_max_extend_steps"]
        super().__init__(
            pvs=dict(pvs or {}), scan_params=full_params, row={},
            simulate=simulate, skip_mirror=True, mirror_stages=mirror_stages,
            confirm_mode=False, enabled=enabled)

        # pv name -> (lo, hi, axis_label); checked by the _write() override.
        self._travel_window = {}
        # None: m_pitch/m_pitch2 never ran or never reached a decision.
        # True: restored to its pre-scan value after a failed/refused scan.
        # False: moved to its scan result and left there.
        self._pitch_restored = None
        self._pitch_touched = False
        self._final_reported = False
        self._steps_run = []
        self._steps_skipped = {}
        self._record = {}

    # ── config-parameter helpers (never let a bad JSON value raise) ─────────

    def _pf(self, key, default):
        return app.coerce_float(self.params.get(key, default), float(default))

    def _pi(self, key, default):
        return int(app.coerce_float(self.params.get(key, default), float(default)))

    # ── required PVs: the small, DCM-piezo-free set ─────────────────────────

    def _required_pvs(self):
        """PVs this run touches. Deliberately excludes piezo_pitch/piezo_roll
        (the DCM piezos, which this tool exists specifically to not need) and
        every mirror-stage, undulator, mono-energy or slit PV the full console
        would otherwise demand.
        """
        pvs = self.pvs
        out = []

        def add(label, name, try_rbv=False, writable=False):
            out.append((label, (name or "").strip(), try_rbv, writable))

        add("DCM pitch motor",    pvs.get("pitch"), try_rbv=True, writable=True)
        add("DCM roll motor",     pvs.get("roll"),  try_rbv=True, writable=True)
        add("Mirror pitch piezo", pvs.get("mir_piezo_pitch"), writable=True)
        add("V feedback",         pvs.get("feedback_v"), writable=True)
        if (pvs.get("auto_feedback") or "").strip():
            add("Auto Feedback On", pvs["auto_feedback"], writable=True)
        add("Upstream shutter",   pvs.get("shutter_blocking"))
        add("Ion chamber",        pvs.get("ion_chamber"))
        add("BPM X",              pvs.get("bpm_x"))
        add("BPM Y",              pvs.get("bpm_y"))
        add("BPM intensity",      pvs.get("bpm_intensity"))
        if (pvs.get("ic_sen_unit") or "").strip():
            add("IC sensitivity unit", pvs["ic_sen_unit"])
        if (pvs.get("ic_sen_num") or "").strip():
            add("IC sensitivity num", pvs["ic_sen_num"])
        # State stamp only -- see _write(), this key is never passed to it.
        add("H feedback (state only)", pvs.get("feedback_h"))

        seen, uniq = set(), []
        for row in out:
            _label, name, _try_rbv, _writable = row
            if name and name in seen:
                continue
            if name:
                seen.add(name)
            uniq.append(row)
        return uniq

    # ── travel-window guard: refuse, never clamp ────────────────────────────

    def _declare_window(self, pv, live, excursion, axis_label):
        name = (pv or "").strip()
        if not name:
            return
        lo, hi = live - abs(excursion), live + abs(excursion)
        self._travel_window[name] = (lo, hi, axis_label)
        self.log("  %s: travel window for this step is [%.6g, %.6g] "
                 "(live %.6g +/- %.6g)." % (axis_label, lo, hi, live, abs(excursion)))

    def _write(self, pv_name, value, context, wait=None):
        """Adds one guard ahead of the inherited write: any value that would
        leave the declared travel window for that PV is refused outright,
        never clamped. A clamped write would still succeed but leave the
        motor somewhere other than the value recorded and plotted, which is
        exactly the silent-lie failure mode this guard exists to prevent.
        """
        name = (pv_name or "").strip()
        window = self._travel_window.get(name)
        if window is not None:
            lo, hi, axis_label = window
            try:
                v = float(value)
            except (TypeError, ValueError):
                v = None
            if v is not None and not (lo - 1e-9 <= v <= hi + 1e-9):
                reason = ("%s: a write of %.6g to %s would leave the declared "
                          "travel window [%.6g, %.6g] for this run."
                          % (axis_label, v, name, lo, hi))
                self.log("CRITICAL — TRAVEL WINDOW: %s" % reason, "error")
                self.log("  While: %s" % context, "error")
                self.log("  Refusing — out-of-window writes are never "
                         "clamped. Nothing was written.", "error")
                raise app.PVFaultAbort(name, context)
        return super()._write(pv_name, value, context, wait=wait)

    # ── limit guard (section 5): the real search budget vs the record's own
    #    drive limits, using .RTYP to pick which limit fields to read ───────

    def _check_travel_budget(self, pv, start, budget, axis_label):
        """Refuse up front if [start-budget, start+budget] would exceed the
        PV's own drive limits. Returns True if the scan may proceed.

        Skipped entirely in simulation: EpicsInterface.get() returns
        _sim_vals.get(pv, 0.0) there, so both limit fields would read 0.0 and
        a naive guard would refuse every simulated run.

        Reads the raw fields via self.epics.get(), never _read_float(): a
        record legitimately missing .DRVH/.DRVL (or .HLM/.LLM) must read as
        "cannot check", not turn into a blocking PV fault on the very guard
        meant to protect the run.
        """
        if self.simulate:
            return True
        name = (pv or "").strip()
        if not name:
            return True
        rtyp = (getattr(self, "_rtype", None) or {}).get(name)
        if rtyp:
            is_motor = rtyp.strip().lower() == "motor"
        else:
            is_motor = (getattr(self, "_is_motor", {}) or {}).get(name)
            if is_motor is None:
                is_motor = self._pv_is_motor(name)
        lo_field, hi_field = (".LLM", ".HLM") if is_motor else (".DRVL", ".DRVH")
        lo_raw = self.epics.get(name + lo_field)
        hi_raw = self.epics.get(name + hi_field)
        try:
            lo_v, hi_v = float(lo_raw), float(hi_raw)
        except (TypeError, ValueError):
            self.log("  %s: could not read %s%s / %s%s — cannot check the "
                     "drive limits, proceeding." % (axis_label, name, lo_field,
                                                    name, hi_field), "warn")
            return True
        if hi_v <= lo_v:
            self.log("  %s: %s drive limits read as [%.6g, %.6g] (not "
                     "configured in EPICS) — cannot check, proceeding."
                     % (axis_label, name, lo_v, hi_v), "warn")
            return True
        want_lo, want_hi = start - abs(budget), start + abs(budget)
        if want_lo < lo_v or want_hi > hi_v:
            self.log("CRITICAL: %s search window [%.6g, %.6g] (start %.6g +/- "
                     "budget %.6g) would exceed %s's drive limits [%.6g, "
                     "%.6g]. Refusing to scan." % (axis_label, want_lo, want_hi,
                                                   start, abs(budget), name,
                                                   lo_v, hi_v), "error")
            return False
        self.log("  %s: search window [%.6g, %.6g] fits within %s's drive "
                 "limits [%.6g, %.6g]." % (axis_label, want_lo, want_hi, name,
                                           lo_v, hi_v), "ok")
        return True

    # ── mirror-in guard, biased toward "in" ──────────────────────────────────

    def _mirror_stage_info(self, name_substr):
        for stage in self.mirror_stages:
            if name_substr in (stage.get("name") or ""):
                return stage
        # Same hard-coded fallback _mirror_yz_pvs() uses, so pre-flight and
        # this guard always agree about which PV they mean.
        fallback = {
            "VDM": {"pv": "ID15A1:DMS:VDM:Y", "val_in": 0.0, "val_out": 3000.0},
            "VFM": {"pv": "ID15A1:DMS:VFM:Y", "val_in": 0.0, "val_out": -3000.0},
        }
        return fallback.get(name_substr)

    def _check_mirror_in(self):
        """Returns "verified_in" | "verified_out" | "could_not_check".

        Classifies by fraction of the way travelled from val_in to val_out
        rather than by absolute position, so an unfamiliar in/out convention
        cannot be misread as "out". A stage that will not read counts as
        unknown, never as out: an unreachable readback must not block a run
        that never moves the mirror stages itself.
        """
        if self.simulate:
            return "verified_in"
        threshold = self._pf("mini_mirror_out_fraction", 0.5)
        fracs = []
        for substr in ("VDM", "VFM"):
            info = self._mirror_stage_info(substr)
            if info is None:
                continue
            pv = (info.get("pv") or "").strip()
            if not pv:
                continue
            val = self.epics.get(pv)
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            val_in, val_out = float(info.get("val_in", 0.0)), float(info.get("val_out", 0.0))
            span = val_out - val_in
            if abs(span) < 1e-9:
                continue
            frac = (val - val_in) / span
            fracs.append(frac)
            self.log("  Mirror stage %s:Y reads %.6g (%.1f%% of the way from "
                     "in to out)." % (substr, val, frac * 100.0))
        if any(f >= threshold for f in fracs):
            return "verified_out"
        if fracs:
            return "verified_in"
        return "could_not_check"

    # ── one pitch-motor peak scan, reused for m_pitch and m_pitch2 ──────────

    def _run_pitch_scan(self, substep_key):
        """Scan the DCM pitch motor for the ion-chamber intensity peak, then
        explicitly write the motor to the returned peak -- _smart_scan_peak
        only fits and reports it, it does not drive the motor, and both
        existing callers in the full console write explicitly afterward.
        Forgetting that would scan for the peak and leave the DCM detuned.
        """
        self.substep_status.emit(substep_key, "running")
        self._pre_scan(substep_key, allow_h=True)
        pitch_pv = (self.pvs.get("pitch") or "").strip()
        live = self._read_float(pitch_pv, "%s — read live DCM pitch" % substep_key)
        half_width = self._pf("mini_pitch_half_width", 0.05)
        steps      = max(self._pi("mini_pitch_steps", 25), 5)
        excursion  = self._pf("mini_pitch_max_excursion", 0.5)
        self._declare_window(pitch_pv, live, excursion, "DCM pitch")
        self._pitch_touched = True
        self._record["dcm_pitch_start_urad"] = live

        # Anchor the simulated peak near the live read, inside the coarse
        # window -- the same discipline the full app's 5C sim uses -- so an
        # unseeded simulated run cannot fit a peak nowhere near a real one.
        true_peak = live + random.uniform(-0.3, 0.3) * half_width

        def _sim_fn(a, b, n):
            return app.sim_scan_pitch(a, b, n, true_peak)

        # Tap the existing scan_point stream for the initial coarse pass
        # rather than re-scanning: _smart_scan_peak already emits every point
        # it samples, in acquisition order, so the first `steps` of them ARE
        # the coarse pass this contrast/saturation check needs.
        coarse = []

        def _capture(key, x, y):
            if key == substep_key and len(coarse) < steps:
                coarse.append((x, y))

        self.scan_point.connect(_capture)
        try:
            peak, sigma = self._smart_scan_peak(
                pitch_pv, center=live, half_range=half_width, steps=steps,
                sim_fn=_sim_fn, substep_key=substep_key)
        finally:
            try:
                self.scan_point.disconnect(_capture)
            except TypeError:
                pass

        if self._abort:
            return False

        ys = [y for _x, y in coarse]
        if ys:
            contrast = max(ys) - min(ys)
            min_contrast = self._pf("mini_min_contrast", 0.05)
            if contrast < min_contrast:
                self.log("  REFUSING %s: the coarse pass spans only %.4g "
                         "(mini_min_contrast is %.4g) — this looks like a "
                         "flat or dead detector, not a peak." %
                         (substep_key, contrast, min_contrast), "error")
                self._write(pitch_pv, live, "%s — restore pre-scan pitch" % substep_key)
                self._pitch_restored = True
                return False
            top = max(ys)
            near_top = sum(1 for y in ys if (top - y) <= 0.01 * max(contrast, 1e-9))
            if len(ys) >= 5 and near_top / len(ys) > 0.3:
                self.log("  REFUSING %s: %d of %d coarse-pass points sit "
                         "within 1%% of the maximum — a flat-topped peak "
                         "usually means the ion chamber range is saturated. "
                         "Check ic_sen_unit / ic_sen_num before retrying." %
                         (substep_key, near_top, len(ys)), "error")
                self._write(pitch_pv, live, "%s — restore pre-scan pitch" % substep_key)
                self._pitch_restored = True
                return False

        if peak is None:
            self.log("  INSUFFICIENT DATA in %s: there is no energy-table row "
                     "to fall back to here (the mini run has none), so the "
                     "pitch is restored to its pre-scan value and the run "
                     "stops." % substep_key, "error")
            self._write(pitch_pv, live, "%s — restore pre-scan pitch" % substep_key)
            self._pitch_restored = True
            return False

        self._write(pitch_pv, peak, "%s — move pitch to the intensity peak" % substep_key)
        self._pitch_restored = False
        self._record["dcm_pitch_urad"] = peak
        self._record["dcm_pitch_sigma_urad"] = sigma
        self._record["restored"] = False
        self.log("  %s: intensity peak at pitch = %.6f -> moved." % (substep_key, peak), "ok")
        self.substep_status.emit(substep_key, "done")
        return True

    # ── roll scan (conditional) ──────────────────────────────────────────────

    def _run_roll_scan(self, substep_key, bpm_x_live):
        self.substep_status.emit(substep_key, "running")
        self._pre_scan(substep_key, allow_h=True)
        roll_pv  = (self.pvs.get("roll") or "").strip()
        bpm_x_pv = (self.pvs.get("bpm_x") or "").strip()
        live = self._read_float(roll_pv, "%s — read live DCM roll" % substep_key)
        roll_start = self._pf("mini_roll_start", -0.05)
        roll_step  = self._pf("mini_roll_step", 0.005)
        excursion  = self._pf("mini_roll_max_excursion", 0.5)
        start = live + roll_start

        if not self._check_travel_budget(roll_pv, start, excursion, "DCM roll"):
            return False   # already logged; nothing was written

        self._declare_window(roll_pv, live, excursion, "DCM roll")
        self._record["dcm_roll_start_urad"] = live

        # Anchor the simulated crossing a handful of steps from `start`,
        # never near the far edge of the safety excursion: _scan_to_zero
        # walks with no fixed end, and an unseeded or far-anchored simulated
        # crossing extends the search budget on every retry and would spin
        # forever in simulation (where _fault() always answers "retry").
        sign = 1.0 if roll_step >= 0 else -1.0
        nominal_span = max(abs(roll_step) * 20.0, 1e-6)
        true_zero = start + sign * nominal_span * random.uniform(0.2, 0.6)

        def _sim_fn(x):
            return app.sim_zero_line(x, true_zero, slope=10.0)

        zero = self._scan_to_zero(
            roll_pv, bpm_x_pv, start=start, step=roll_step, substep_key=substep_key,
            max_span=excursion, sim_fn=_sim_fn, signal_label="BPM X")

        if self._abort:
            return False
        if zero is None:
            self.log("  %s: stopped before a BPM x crossing was found." % substep_key, "error")
            return False

        # _scan_to_zero returns the INTERPOLATED crossing but does not drive
        # the motor there itself -- it leaves the motor at the last stepped
        # point, up to one step size away. The full console's own 3C and 5C
        # callers write the returned value explicitly for exactly this
        # reason; this mirrors that, not the "no explicit write needed"
        # reading of the design note (see the final report for why).
        self._write(roll_pv, zero, "%s — move roll to the BPM x zero-crossing" % substep_key)
        self.log("  %s: BPM x zero-crossing at roll = %.6f -> moved." % (substep_key, zero), "ok")
        self._record["dcm_roll_urad"] = zero
        self.substep_status.emit(substep_key, "done")
        return True

    # ── mirror piezo scan (pre-positioning only) ────────────────────────────

    def _run_piezo_scan(self, substep_key):
        self.substep_status.emit(substep_key, "running")
        self._pre_scan(substep_key, allow_h=True)
        piezo_pv = (self.pvs.get("mir_piezo_pitch") or "").strip()
        bpm_y_pv = (self.pvs.get("bpm_y") or "").strip()
        if not piezo_pv:
            self.log("  %s: mirror piezo pitch PV is not configured — "
                     "cannot scan." % substep_key, "error")
            return False
        live = self._read_float(piezo_pv, "%s — read live mirror piezo pitch" % substep_key)
        piezo_start = self._pf("mini_piezo_start", -1.0)
        piezo_step  = self._pf("mini_piezo_step", 0.1)
        excursion   = self._pf("mini_piezo_max_excursion", 3.0)
        start = live + piezo_start

        if not self._check_travel_budget(piezo_pv, start, excursion, "Mirror piezo pitch"):
            return False

        self._declare_window(piezo_pv, live, excursion, "Mirror piezo pitch")

        sign = 1.0 if piezo_step >= 0 else -1.0
        nominal_span = max(abs(piezo_step) * 20.0, 1e-6)
        true_zero = start + sign * nominal_span * random.uniform(0.2, 0.6)

        def _sim_fn(x):
            return app.sim_zero_line(x, true_zero, slope=0.5, noise=0.002)

        zero = self._scan_to_zero(
            piezo_pv, bpm_y_pv, start=start, step=piezo_step, substep_key=substep_key,
            max_span=excursion, sim_fn=_sim_fn, signal_label="BPM Y")

        if self._abort:
            return False
        if zero is None:
            self.log("  %s: stopped before a BPM y crossing was found." % substep_key, "error")
            return False

        self._write(piezo_pv, zero,
                    "%s — move mirror piezo to the BPM Y zero-crossing" % substep_key)
        self.log("  %s: BPM y zero-crossing at mirror piezo = %.6f -> moved "
                 "(pre-positioned for V lock — V feedback NOT engaged)."
                 % (substep_key, zero), "ok")
        self._record["mir_piezo_dcom"] = zero
        self.substep_status.emit(substep_key, "done")
        return True

    # ── the sequence itself ──────────────────────────────────────────────────

    def _run_sequence(self):
        """Called from run() (inherited, unchanged). Pre-flight MUST be the
        first act here: run() only attaches the CA context and catches
        PVFaultAbort, so an override that forgot this line would silently
        skip the connect test on a run that then switches V feedback off.
        """
        self._preflight()
        try:
            self._run_body()
        finally:
            # Runs on every exit path: success, PVFaultAbort propagating out
            # of _run_body, any other exception, and operator abort (which
            # returns out of _run_body normally via _abort_cleanup()). This is
            # deliberately the ONLY place that reports the V-feedback state,
            # so there is exactly one authoritative line per run rather than
            # a chance of two disagreeing ones.
            self._report_final_state()

    def _run_body(self):
        pvs = self.pvs
        self.bpm_update.emit(0.0, 0.0, 0.0)
        self.feedback_update.emit(False, False)

        # ── Section 10: the ion chamber is the ion chamber ──
        ion_pv = (pvs.get("ion_chamber") or "").strip()
        sig_pv = self._signal_pv("dcm_signal")
        if not ion_pv or sig_pv != ion_pv:
            self.log("CRITICAL: no usable ion-chamber PV is configured. The "
                     "mini alignment refuses to fall back to the BPM for its "
                     "peak scan. Set 'Ion Chamber' in the full console's "
                     "Setup tab (Global) first — report_PV_list.docx "
                     "lists 'MonP Max Intensity' as 15IDC:scaler1.S3 as a "
                     "likely candidate to confirm, not to assume.", "error")
            return self._abort_cleanup("refused: no ion chamber PV configured")

        self._record["signal_pv"] = ion_pv
        if (pvs.get("ic_sen_unit") or "").strip():
            self._record["ic_sen_unit"] = self.epics.get(pvs["ic_sen_unit"], as_string=True)
        if (pvs.get("ic_sen_num") or "").strip():
            self._record["ic_sen_num"] = self.epics.get(pvs["ic_sen_num"], as_string=True)

        # ── mirror-in guard ──
        mirror_state = self._check_mirror_in()
        self._record["mirror_in"] = mirror_state
        if mirror_state == "verified_out":
            self.log("CRITICAL: the mirror stages read as OUT. This sequence "
                     "assumes the mirror is IN for its whole run and never "
                     "inserts it itself. Insert the mirror from the main "
                     "console, then retry.", "error")
            return self._abort_cleanup("refused: mirror verified out")
        elif mirror_state == "could_not_check":
            self.log("  WARNING: could not verify the mirror is in (VDM:Y / "
                     "VFM:Y did not answer). Proceeding, since an unreachable "
                     "readback must not block a run that never moves those "
                     "stages.", "warn")
        else:
            self.log("  Mirror verified IN. (With the mirror in, the pitch "
                     "peak is the rocking curve convolved with the mirror's "
                     "angular acceptance, not the mirror-out peak the full "
                     "console's 3B measures.)", "ok")

        # ── m_voff: always runs, no tick box ──
        h_pv = (pvs.get("feedback_h") or "").strip()
        h_state = False
        if h_pv:
            h_val = self._read_float(h_pv, "m_voff — read H feedback state (record only)",
                                     allow_blank=True, default=0.0)
            h_state = bool(h_val)
        self._record["feedback_h_on"] = h_state
        auto_pv = (pvs.get("auto_feedback") or "").strip()
        if auto_pv:
            self._write(auto_pv, 0, "m_voff — clear the AutoFeedback override")
        v_pv = (pvs.get("feedback_v") or "").strip()
        self._write(v_pv, 0, "m_voff — force V feedback off")
        self.feedback_update.emit(h_state, False)
        self.log("  V feedback -> OFF. (This beamline has no DCM piezo: V "
                 "stays off for the whole run. Step 4 pre-positions the "
                 "mirror piezo -- it does not close the loop.)", "ok")
        if self._abort:
            return self._abort_cleanup()

        bpm_x_pv = (pvs.get("bpm_x") or "").strip()
        bpm_y_pv = (pvs.get("bpm_y") or "").strip()
        bpm_i_pv = (pvs.get("bpm_intensity") or "").strip()

        # ── m_pitch ──
        if self._skip("m_pitch"):
            self._steps_skipped["m_pitch"] = "operator_unticked"
        else:
            if not self._run_pitch_scan("m_pitch"):
                return self._abort_cleanup("stopped in the pitch scan — see log")
            self._steps_run.append("m_pitch")
        if self._abort:
            return self._abort_cleanup()

        bpm_x_before = self._read_float(bpm_x_pv, "post-pitch — read BPM x",
                                        allow_blank=True, default=0.0)
        bpm_y_before = self._read_float(bpm_y_pv, "post-pitch — read BPM y",
                                        allow_blank=True, default=0.0)
        bpm_i_before = self._read_float(bpm_i_pv, "post-pitch — read BPM intensity",
                                        allow_blank=True, default=0.0)
        self._record["bpm_x_before_um"] = bpm_x_before
        self._record["bpm_y_before_um"] = bpm_y_before
        self.bpm_update.emit(bpm_x_before, bpm_y_before, bpm_i_before)
        self.log("  After the pitch step: BPM x = %.4g um, BPM y = %.4g um."
                 % (bpm_x_before, bpm_y_before))

        # ── m_roll: the first "skip because already good" logic, so the
        #    decision is always logged with the measured number, and
        #    "already centred" is recorded distinctly from "operator
        #    unticked it" ──
        threshold = self._pf("mini_bpm_x_threshold_um", 12.0)
        self._record["bpm_x_threshold_um"] = threshold
        roll_ran = False
        if self._skip("m_roll"):
            self._steps_skipped["m_roll"] = "operator_unticked"
            self._record["roll_skipped_reason"] = "operator_unticked"
        else:
            bpm_x_now = self._read_float(bpm_x_pv, "m_roll — read BPM x for the threshold test")
            if abs(bpm_x_now) <= threshold:
                self.log("  BPM x = %.4g um, within the %.4g um threshold "
                         "— roll scan not needed." % (bpm_x_now, threshold), "ok")
                self.substep_status.emit("m_roll", "skipped")
                self._steps_skipped["m_roll"] = "within_threshold"
                self._record["roll_skipped_reason"] = "within_threshold"
            else:
                self.log("  BPM x = %.4g um, outside the %.4g um threshold "
                         "— scanning DCM roll." % (bpm_x_now, threshold), "warn")
                if not self._run_roll_scan("m_roll", bpm_x_now):
                    return self._abort_cleanup("stopped in the roll scan — see log")
                self._steps_run.append("m_roll")
                roll_ran = True
        if self._abort:
            return self._abort_cleanup()

        # ── m_pitch2: optional 3D-equivalent re-peak, default off, and
        #    automatically skipped whenever m_roll did not move anything ──
        if not roll_ran:
            self.substep_status.emit("m_pitch2", "skipped")
            self.log("  m_pitch2 skipped — the roll scan did not move "
                     "anything, so there is nothing to re-peak for.", "info")
            self._steps_skipped["m_pitch2"] = "no_roll_move"
        elif self._skip("m_pitch2"):
            self._steps_skipped["m_pitch2"] = "operator_unticked"
        else:
            if not self._run_pitch_scan("m_pitch2"):
                return self._abort_cleanup("stopped in the pitch re-peak — see log")
            self._steps_run.append("m_pitch2")
        if self._abort:
            return self._abort_cleanup()

        # ── m_piezo ──
        if self._skip("m_piezo"):
            self._steps_skipped["m_piezo"] = "operator_unticked"
        else:
            if not self._run_piezo_scan("m_piezo"):
                return self._abort_cleanup("stopped in the mirror piezo scan — see log")
            self._steps_run.append("m_piezo")
        if self._abort:
            return self._abort_cleanup()

        bpm_x_after = self._read_float(bpm_x_pv, "final — read BPM x", allow_blank=True, default=0.0)
        bpm_y_after = self._read_float(bpm_y_pv, "final — read BPM y", allow_blank=True, default=0.0)
        bpm_i_after = self._read_float(bpm_i_pv, "final — read BPM intensity", allow_blank=True, default=0.0)
        self._record["bpm_x_after_um"] = bpm_x_after
        self._record["bpm_y_after_um"] = bpm_y_after
        self._record["bpm_intensity"] = bpm_i_after
        self.bpm_update.emit(bpm_x_after, bpm_y_after, bpm_i_after)

        outcome = "pre-positioned for V lock — V feedback NOT engaged"
        self._record["outcome"] = outcome
        self._record["feedback_v_engaged"] = False
        self._record["steps_run"] = list(self._steps_run)
        self._record["steps_skipped"] = dict(self._steps_skipped)
        self.log("Mini alignment finished — %s." % outcome, "ok")
        self.finished.emit(True)

    def _report_final_state(self):
        """Idempotent: safe to reach via more than one exit path without
        printing two disagreeing lines. Reports where the pitch motor is and
        whether it was restored, and states in words that V feedback was not
        engaged -- the single most important line on a run most likely to
        have left something mid-scan.
        """
        if self._final_reported:
            return
        self._final_reported = True

        pitch_pv = (self.pvs.get("pitch") or "").strip()
        pitch_now = None
        if pitch_pv:
            try:
                raw = self.epics.get(pitch_pv)
                pitch_now = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                pitch_now = None

        if self._pitch_restored is True:
            restored_txt = "restored to its pre-scan value"
        elif self._pitch_restored is False:
            restored_txt = "moved to its scan result and left there"
        elif self._pitch_touched:
            restored_txt = ("may have moved during an interrupted scan; "
                            "not confirmed restored — check before using the beam")
        else:
            restored_txt = "not touched by this run"

        pos_txt = ("%.6f" % pitch_now) if pitch_now is not None else "unknown (could not read)"
        self.log("━━ Vertical feedback is OFF — NOT engaged by "
                 "this run. ━━", "warn")
        self.log("  DCM pitch is %s; current reading: %s." % (restored_txt, pos_txt), "warn")

        self._record.setdefault("feedback_v_engaged", False)
        self._record.setdefault("outcome", "stopped before completing — see log")
        self._record.setdefault("steps_run", list(self._steps_run))
        self._record.setdefault("steps_skipped", dict(self._steps_skipped))
        self._record.setdefault("restored", bool(self._pitch_restored))
        self._record["dcm_pitch_final_urad"] = pitch_now
        self._record["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._record["simulate"] = self.simulate
        self.record_ready.emit(dict(self._record))


# ─── Idle-time BPM monitor ─────────────────────────────────────────────────────
class MiniBpmMonitor(QObject):
    """Idle-time BPM x/y/intensity readback via CA monitors, modelled on
    SetupTab._subscribe_pv.

    Suspends bpm_x / bpm_y while a run is in progress. EpicsInterface.get()
    calls epics.caget() without use_monitor=False, so pyepics' default
    use_monitor=True applies: a live auto_monitor=True subscription in this
    same process and CA context would hand a worker read back the CACHED
    monitor value after only the piezo settle time (0.2 s), not a fresh one.
    bpm_x is both the step-3 threshold test and its scan signal, and bpm_y is
    the step-4 scan signal, so a stale read there could skip or misdrive a
    scan. bpm_intensity is readout-only and keeps monitoring throughout.

    Deliberately does not route a disconnect into worker.external_fault: a
    BPM dropping mid-scan is already a hard fault via _read_float returning
    None. A disconnect here just turns the readout red.
    """

    x_changed = pyqtSignal(object)   # float, or None for disconnected
    y_changed = pyqtSignal(object)
    i_changed = pyqtSignal(object)

    def __init__(self, is_running_fn, parent=None):
        super().__init__(parent)
        self._is_running = is_running_fn
        self._pvs = {}
        self._monitored = {}

    def set_pvs(self, pvs):
        self._pvs = dict(pvs or {})

    def start(self, simulate):
        self.stop()
        if simulate or not app.EPICS_AVAILABLE:
            return
        try:
            import epics as _epics
        except ImportError:
            return
        for key, sig, gate in (("bpm_x", self.x_changed, True),
                               ("bpm_y", self.y_changed, True),
                               ("bpm_intensity", self.i_changed, False)):
            name = (self._pvs.get(key) or "").strip()
            if not name:
                continue

            def _cb(value=None, pvname=None, _sig=sig, _gate=gate, **_kw):
                if _gate and self._is_running():
                    return   # the worker owns this readback during a run
                if value is None:
                    return
                try:
                    _sig.emit(float(value))
                except (TypeError, ValueError):
                    pass

            def _conn(pvname=None, conn=None, _sig=sig, **_kw):
                if not conn:
                    _sig.emit(None)

            try:
                pv = _epics.PV(name, callback=_cb, connection_callback=_conn,
                               auto_monitor=True)
                self._monitored[key] = pv
            except Exception:
                pass

    def stop(self):
        for pv in self._monitored.values():
            try:
                pv.disconnect()
            except Exception:
                pass
        self._monitored.clear()


# ─── Window ─────────────────────────────────────────────────────────────────────
class MiniWindow(QMainWindow):
    """Standalone mini console. Constructible from a bare QApplication with no
    arguments and no network, so tests/_harness.py can drive it headlessly.
    """

    alignment_done = pyqtSignal(bool)

    _RUN_LOCK_APP_NAME = "DCM Mini Alignment"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DCM Mini Alignment")
        self.resize(900, 760)
        # app.build_qss(app.PAL) is called fresh here, never the frozen
        # module-level app.QSS constant, which is baked to the Ocean Light
        # palette at import time.
        self.setStyleSheet(app.build_qss(app.PAL))

        self._worker = None
        self._thread = None
        self._running = False
        self._holds_run_lock = False
        self._faulted = False
        self._fault_dlg = None
        self._fault_rows = []
        self._step_chk = {}
        self._step_tag = {}
        self._pvs = dict(app.DEFAULT_PVS)
        self._scan_base = dict(app.DEFAULT_SCAN)
        self._mirror_stages = list(app.DEFAULT_MIRROR_STAGES)
        self._mini_scan = dict(MINI_DEFAULT_SCAN)
        self._history_rows = []

        self._models = {
            fig_id: app.FigureModel(fig_id, device, tab_text, title, xl, yl, fill)
            for fig_id, device, tab_text, title, xl, yl, fill in _MINI_FIGURE_DEFS
        }

        self._bpm_monitor = MiniBpmMonitor(lambda: self._running)
        self._bpm_monitor.x_changed.connect(self._on_idle_bpm_x)
        self._bpm_monitor.y_changed.connect(self._on_idle_bpm_y)
        self._bpm_monitor.i_changed.connect(self._on_idle_bpm_i)

        self._build()
        self._load_initial_state()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        title = QLabel("DCM Mini Alignment")
        title.setStyleSheet("font-size:15px; font-weight:700; color:%s;" % app.PAL["text_pri"])
        top.addWidget(title)
        sub = QLabel("No DCM piezo — pitch/roll motor + mirror piezo only")
        sub.setStyleSheet("color:%s; font-size:10px;" % app.PAL["text_dim"])
        title_col = QVBoxLayout()
        title_col.addWidget(title)
        title_col.addWidget(sub)
        top.addLayout(title_col)
        top.addStretch()
        self._mode_tag = app.make_tag(
            "EPICS available" if app.EPICS_AVAILABLE else "Simulation mode",
            "green" if app.EPICS_AVAILABLE else "amber")
        top.addWidget(self._mode_tag)
        self.sim_chk = QCheckBox("Simulation")
        top.addWidget(self.sim_chk)
        self.start_btn = app.styled_button("▶  Start", "primary")
        self.abort_btn = app.styled_button("■  Abort", "danger")
        self.abort_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._on_start)
        self.abort_btn.clicked.connect(self._on_abort)
        top.addWidget(self.start_btn)
        top.addWidget(self.abort_btn)
        root.addLayout(top)

        # Fault banner, wired exactly like AlignmentTab's: show(), not exec(),
        # refresh() on a repeat fault, a Review-fault button to reopen a
        # dismissed dialog. _fault() blocks the worker thread indefinitely
        # until this is wired, so it is not optional.
        self.fault_banner = QWidget()
        self.fault_banner.setVisible(False)
        fb = QVBoxLayout(self.fault_banner)
        fb.setContentsMargins(8, 6, 8, 6)
        self.fault_tag = QLabel()
        self.fault_tag.setWordWrap(True)
        self.fault_pv_lbl = QLabel()
        self.fault_pv_lbl.setWordWrap(True)
        self.fault_btn = app.styled_button("Review fault…")
        self.fault_btn.clicked.connect(self._open_fault_dialog)
        fb.addWidget(self.fault_tag)
        fb.addWidget(self.fault_pv_lbl)
        fb.addWidget(self.fault_btn)
        root.addWidget(self.fault_banner)

        steps_box = QGroupBox("Steps")
        steps_lay = QVBoxLayout(steps_box)
        for key in _MINI_STEP_ORDER:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 2, 4, 2)
            chk = QCheckBox()
            chk.setChecked(key != "m_pitch2")   # m_pitch2 defaults off
            self._step_chk[key] = chk
            lbl = QLabel(_MINI_STEP_TEXT[key])
            lbl.setWordWrap(True)
            tag = app.make_tag("Idle", "grey")
            self._step_tag[key] = tag
            rl.addWidget(chk)
            rl.addWidget(lbl, 1)
            rl.addWidget(tag)
            steps_lay.addWidget(row)
        root.addWidget(steps_box)

        bpm_box = QGroupBox("Live BPM")
        bpm_lay = QHBoxLayout(bpm_box)
        self._bpm_labels = {}
        for key, label, unit in (("bpm_x", "BPM X", "um"), ("bpm_y", "BPM Y", "um"),
                                 ("bpm_i", "Intensity", "a.u.")):
            w, val = app.make_readout(label, "—", unit)
            self._bpm_labels[key] = val
            bpm_lay.addWidget(w)
        root.addWidget(bpm_box)

        self._fig_pane = app.FigurePane("Mini", list(self._models.values()))
        # FigurePane elides tab text, which earns its keep in the main console
        # where two panes share a splitter. Here there is one full-width pane
        # with three short labels, and eliding rendered them "Pit… / R… / Pie…".
        self._fig_pane.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        root.addWidget(self._fig_pane, 1)

        self.log = app.LogWidget()
        root.addWidget(self.log)

        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(
            ["Time", "Outcome", "Pitch (urad)", "Roll skip reason",
             "BPM x after (um)", "BPM y after (um)"])
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history.setMaximumHeight(150)
        root.addWidget(self.history)

    # ── config load / save ──────────────────────────────────────────────────

    def _load_initial_state(self):
        cfg = app.load_config_file() if hasattr(app, "load_config_file") else {}
        self._pvs = dict(app.DEFAULT_PVS)
        self._pvs.update(cfg.get("pvs", {}) or {})
        self._scan_base = dict(app.DEFAULT_SCAN)
        self._scan_base.update(cfg.get("scan", {}) or {})
        self._mirror_stages = cfg.get("mirror_stages") or list(app.DEFAULT_MIRROR_STAGES)

        mini_cfg = cfg.get("mini", {}) or {}
        mini_scan = dict(MINI_DEFAULT_SCAN)
        mini_scan.update(mini_cfg.get("scan", {}) or {})
        self._mini_scan = mini_scan
        self._step_chk["m_pitch2"].setChecked(
            bool(mini_scan.get("mini_repeak_pitch_after_roll", False)))

        self._history_rows = list(mini_cfg.get("records", []) or [])
        for row in self._history_rows[-20:]:
            self._append_history_row(row)

        self.sim_chk.setChecked(bool(cfg.get("simulate", True)))
        self._bpm_monitor.set_pvs(self._pvs)
        if not self.sim_chk.isChecked() and app.EPICS_AVAILABLE:
            self._bpm_monitor.start(False)

    # ── run-lock handshake, mirroring AlignmentTab._acquire_run_lock_or_refuse ──

    def _acquire_run_lock_or_refuse(self):
        if not (hasattr(app, "read_run_lock") and hasattr(app, "acquire_run_lock")):
            self.log.append_log("Run-lock helpers were not found in this build "
                                "of dcm_align_app.py — starting without "
                                "the cross-app lock.", "warn")
            self._holds_run_lock = False
            return True
        existing = app.read_run_lock()
        if existing is not None and not existing.get("stale"):
            QMessageBox.warning(
                self, "Alignment already running",
                "A DCM alignment is already running in %s\n"
                "on host %s (pid %s), started %s.\n\n"
                "Wait for it to finish before starting the mini run." % (
                    existing.get("app") or "another instance",
                    existing.get("host") or "an unknown host",
                    existing.get("pid", "?"),
                    existing.get("started") or "an unknown time"))
            return False
        if existing is not None and existing.get("stale"):
            reply = QMessageBox.question(
                self, "Stale run lock",
                "A run lock recorded for %s\non host %s (pid %s, started %s) "
                "is present, but that process is no longer running.\n\n"
                "Override the stale lock and start this run?" % (
                    existing.get("app") or "another instance",
                    existing.get("host") or "an unknown host",
                    existing.get("pid", "?"),
                    existing.get("started") or "an unknown time"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return False
        ok, holder = app.acquire_run_lock(self._RUN_LOCK_APP_NAME)
        if not ok:
            holder = holder or {}
            QMessageBox.warning(
                self, "Alignment already running",
                "Could not claim the run lock — %s just started a run.\n"
                "Wait for it to finish before starting another run here."
                % (holder.get("app") or "another instance"))
            return False
        self._holds_run_lock = True
        return True

    # ── start / abort ────────────────────────────────────────────────────────

    def _on_start(self):
        if self._running:
            QMessageBox.information(self, "Already running",
                                    "A mini run is already in progress.")
            return

        cfg = app.load_config_file() if hasattr(app, "load_config_file") else {}
        pvs = dict(app.DEFAULT_PVS)
        pvs.update(cfg.get("pvs", {}) or {})
        scan = dict(app.DEFAULT_SCAN)
        scan.update(cfg.get("scan", {}) or {})
        mini_cfg = cfg.get("mini", {}) or {}
        mini_scan = dict(MINI_DEFAULT_SCAN)
        mini_scan.update(mini_cfg.get("scan", {}) or {})
        mini_scan["mini_repeak_pitch_after_roll"] = self._step_chk["m_pitch2"].isChecked()
        self._mini_scan = mini_scan
        scan.update(mini_scan)
        mirror_stages = cfg.get("mirror_stages") or list(app.DEFAULT_MIRROR_STAGES)
        simulate = self.sim_chk.isChecked()

        if not (pvs.get("ion_chamber") or "").strip():
            QMessageBox.warning(
                self, "No ion chamber configured",
                "The 'Ion Chamber' PV is blank in the shared config. The "
                "mini alignment refuses to fall back to the BPM for its "
                "peak scan.\n\nSet it in the full console's Setup tab "
                "(Global) first. report_PV_list.docx lists 'MonP Max "
                "Intensity' as 15IDC:scaler1.S3 as a likely candidate to "
                "confirm there — not to assume here.")
            return

        if not self._acquire_run_lock_or_refuse():
            return

        enabled = {"m_voff"}
        for key in ("m_pitch", "m_roll", "m_piezo", "m_pitch2"):
            if self._step_chk[key].isChecked():
                enabled.add(key)

        for model in self._models.values():
            model.clear()
        self.log.clear()
        for key in self._step_tag:
            self._set_step_tag(key, "idle")
        self._clear_fault_ui()

        self._pvs = pvs
        self._bpm_monitor.set_pvs(pvs)
        self._bpm_monitor.stop()

        self._running = True
        self.start_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        for chk in self._step_chk.values():
            chk.setEnabled(False)
        self.sim_chk.setEnabled(False)

        self._thread = QThread()
        self._worker = MiniAlignmentWorker(
            pvs, scan, mirror_stages=mirror_stages, simulate=simulate, enabled=enabled)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_signal.connect(self._on_log)
        self._worker.substep_status.connect(self._on_substep_status)
        self._worker.scan_point.connect(self._on_scan_point)
        self._worker.scan_peak.connect(self._on_scan_peak)
        self._worker.scan_fit.connect(self._on_scan_fit)
        self._worker.bpm_update.connect(self._on_bpm_update)
        self._worker.feedback_update.connect(self._on_feedback)
        self._worker.pv_fault.connect(self._on_pv_fault)
        self._worker.pv_fault_cleared.connect(self._on_pv_fault_cleared)
        self._worker.paused_changed.connect(self._on_paused_changed)
        self._worker.preflight_report.connect(self._on_preflight_report)
        self._worker.record_ready.connect(self._on_record_ready)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_abort(self):
        if self._worker:
            self._worker.abort()

    # ── plain slots ──────────────────────────────────────────────────────────

    def _on_log(self, msg, level):
        self.log.append_log(msg, level)

    def _set_step_tag(self, key, status):
        tag = self._step_tag.get(key)
        if tag is None:
            return
        text, obj = {
            "idle":    ("Idle", "tag_grey"),
            "running": ("Running…", "tag_amber"),
            "done":    ("Done", "tag_green"),
            "skipped": ("Skipped", "tag_grey"),
            "error":   ("Error", "tag_red"),
        }.get(status, (status, "tag_grey"))
        tag.setText(text)
        tag.setObjectName(obj)
        style = tag.style()
        style.unpolish(tag)
        style.polish(tag)

    def _on_substep_status(self, key, status):
        self._set_step_tag(key, status)

    def _route(self, key):
        """_MINI_SCAN_ROUTES entry for a series key, resolved by stripping any
        "#<pass>" suffix _smart_scan_peak's passes 2+ append (m_pitch and
        m_pitch2 are the only mini scans that call it). The label is
        extended to name the pass when the key carries one, mirroring
        ScanPlotBoard._route in the full console."""
        base, _, suffix = key.partition("#")
        route = _MINI_SCAN_ROUTES.get(base)
        if route is None:
            return None
        fig_id, label, marker_kind = route
        if suffix:
            label = f"{label} · pass {suffix}"
        return fig_id, label, marker_kind

    def _on_scan_point(self, key, x, y):
        route = self._route(key)
        if route is None:
            return
        fig_id, label, marker_kind = route
        self._models[fig_id].add_point(key, label, x, y, marker_kind)

    def _on_scan_peak(self, key, value):
        route = self._route(key)
        if route is None:
            return
        fig_id, label, marker_kind = route
        self._models[fig_id].set_marker(key, label, value, marker_kind)

    def _on_scan_fit(self, key, xs, ys):
        route = self._route(key)
        if route is None:
            return
        fig_id, _label, _marker_kind = route
        self._models[fig_id].set_fit(key, xs, ys)

    def _on_bpm_update(self, x, y, intensity):
        self._bpm_labels["bpm_x"].setText("%+.4f" % x)
        self._bpm_labels["bpm_y"].setText("%+.4f" % y)
        self._bpm_labels["bpm_i"].setText("%.3f" % intensity)

    def _on_idle_bpm_x(self, value):
        if self._running:
            return
        self._bpm_labels["bpm_x"].setText("DISCONNECTED" if value is None else "%+.4f" % value)

    def _on_idle_bpm_y(self, value):
        if self._running:
            return
        self._bpm_labels["bpm_y"].setText("DISCONNECTED" if value is None else "%+.4f" % value)

    def _on_idle_bpm_i(self, value):
        self._bpm_labels["bpm_i"].setText("DISCONNECTED" if value is None else "%.3f" % value)

    def _on_feedback(self, h, v):
        # Kept minimal: the log already states the V-off outcome loudly, and
        # this window has no separate H/V tag row to update.
        pass

    def _on_preflight_report(self, rows):
        for label, pv, status in rows:
            level = "ok" if status.startswith("ok") else (
                "info" if status == "skipped" else "error")
            self.log.append_log("    %-28s %-38s %s" % (label, pv or "(none)", status), level)

    # ── PV fault dialog, wired exactly as AlignmentTab ──────────────────────

    def _on_pv_fault(self, pv, context, reason):
        self._faulted = True
        self._fault_rows.append((pv, context, reason,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self._set_fault_banner("paused", pv)
        if self._fault_dlg is not None:
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
        dlg = app.PVFaultDialog(self._fault_rows, self.window())
        dlg.retry_clicked.connect(self._fault_retry)
        dlg.abort_clicked.connect(self._fault_abort)
        dlg.finished.connect(self._on_fault_dlg_finished)
        self._fault_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_fault_dlg_finished(self, _result):
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

    def _set_fault_banner(self, state, pv=""):
        if not state:
            self.fault_banner.setVisible(False)
            return
        paused = (state == "paused")
        self.fault_tag.setText("PAUSED — PV FAULT" if paused
                               else "FAULT DETECTED — pausing at next safe point…")
        self.fault_pv_lbl.setText(pv)
        self.fault_pv_lbl.setVisible(bool(pv))
        self.fault_btn.setVisible(paused)
        self.fault_banner.setVisible(True)

    # ── run record / history ────────────────────────────────────────────────

    def _on_record_ready(self, record):
        self._history_rows.append(record)
        self._append_history_row(record)
        if hasattr(app, "save_config_section"):
            data = {"scan": dict(self._mini_scan), "records": self._history_rows[-100:]}
            ok, reason = app.save_config_section("mini", data)
            if not ok:
                self.log.append_log("Could not save the mini run record: %s" % reason, "warn")
        else:
            self.log.append_log("save_config_section() was not found in "
                                "dcm_align_app.py — the run record was "
                                "not persisted to the config file.", "warn")

    def _append_history_row(self, record):
        r = self.history.rowCount()
        self.history.insertRow(r)
        vals = [
            record.get("timestamp", ""),
            record.get("outcome", ""),
            _fmt(record.get("dcm_pitch_urad", record.get("dcm_pitch_final_urad"))),
            _fmt(record.get("roll_skipped_reason")),
            _fmt(record.get("bpm_x_after_um")),
            _fmt(record.get("bpm_y_after_um")),
        ]
        for c, v in enumerate(vals):
            self.history.setItem(r, c, QTableWidgetItem(str(v)))
        self.history.scrollToBottom()

    # ── finish / teardown ────────────────────────────────────────────────────

    def _on_finished(self, success):
        self._running = False
        if self._holds_run_lock and hasattr(app, "release_run_lock"):
            app.release_run_lock()
        self._holds_run_lock = False
        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        for chk in self._step_chk.values():
            chk.setEnabled(True)
        self.sim_chk.setEnabled(True)
        self._clear_fault_ui()
        if not success:
            for key in self._step_tag:
                if self._step_tag[key].text().startswith("Running"):
                    self._set_step_tag(key, "error")
        worker, thread = self._worker, self._thread
        self._worker, self._thread = None, None
        if thread is not None:
            thread.quit()
            thread.wait()
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        if not self.sim_chk.isChecked() and app.EPICS_AVAILABLE:
            self._bpm_monitor.start(False)
        self.alignment_done.emit(success)

    def closeEvent(self, event):
        if self._running and self._worker is not None:
            reply = QMessageBox.question(
                self, "Alignment in progress",
                "A mini alignment is still running.\nAbort it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.abort()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(5000)
            if self._holds_run_lock and hasattr(app, "release_run_lock"):
                app.release_run_lock()
                self._holds_run_lock = False
        self._bpm_monitor.stop()
        super().closeEvent(event)


# ─── Entry point ─────────────────────────────────────────────────────────────
def main():
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("DCM Mini Alignment")
    qapp.setStyle("Fusion")
    win = MiniWindow()
    win.show()
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
