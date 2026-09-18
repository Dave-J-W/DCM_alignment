# -*- coding: utf-8 -*-
"""Mini alignment console: the safety properties, asserted rather than assumed.

The mini console exists for a beamline with no DCM piezo, and it deliberately
leaves vertical feedback off. That makes a handful of properties load-bearing:
it must drive exactly five PVs and no others, never touch the DCM piezos, never
write outside a declared travel window, and never stay quiet about the feedback
state it left behind. Each of those is checked here.

Written against the module's public behaviour, deliberately not by whoever wrote
the module.

    py -3.9 tests/test_mini.py
"""
import io
import json
import os

import _harness as H

app = H.app
R = H.Report("DCM Mini Alignment Console")

H.silence_dialogs()
qapp = H.qapp()

import dcm_mini_app as mini  # noqa: E402  (after _harness sets the import path)

ION = "TEST:ion_chamber"

# ── transport instrumentation ───────────────────────────────────────────────
# SEED lets a test force a simulated readback (e.g. BPM x well outside the
# threshold); everything else falls through to the real simulated transport.
SEED = {}
WRITES = []          # [(pv, value), ...] in order
READS = []           # every pv name passed to get(), including field suffixes

_real_get = app.EpicsInterface.get
_real_put = app.EpicsInterface.put


def spy_get(self, pv, as_string=False, timeout=3.0):
    READS.append(pv)
    if pv in SEED:
        return SEED[pv]
    return _real_get(self, pv, as_string=as_string, timeout=timeout)


def spy_put(self, pv, value, wait=True, timeout=30.0):
    WRITES.append((pv, value))
    return _real_put(self, pv, value, wait=wait, timeout=timeout)


app.EpicsInterface.get = spy_get
app.EpicsInterface.put = spy_put


def reset_spies():
    del WRITES[:]
    del READS[:]
    SEED.clear()


def write_config(**pv_overrides):
    """Seed the shared config the mini window reads on Start."""
    pvs = dict(app.DEFAULT_PVS)
    pvs["ion_chamber"] = ION
    pvs.update(pv_overrides)
    cfg = {"pvs": pvs, "scan": dict(app.DEFAULT_SCAN), "simulate": True}
    app.atomic_write_json(app.AUTO_CONFIG_PATH, cfg)
    return cfg


def run_window(steps=("m_pitch", "m_roll", "m_piezo"), timeout_ms=120000):
    """Construct the window, run one simulated sequence, return (win, result)."""
    win = mini.MiniWindow()
    win.show()
    win.sim_chk.setChecked(True)
    for key, chk in win._step_chk.items():
        chk.setChecked(key in steps)
    result = {}
    win.alignment_done.connect(lambda ok: result.setdefault("ok", ok))
    win._on_start()
    finished = H.pump(lambda: "ok" in result, timeout_ms)
    return win, result, finished


# ═══ 1. the happy path terminates ══════════════════════════════════════════
# _scan_to_zero has no fixed end: it walks until the signal changes sign, and
# its max_span escape calls _fault, which returns "retry" in simulation and
# extends the budget. A simulated crossing that is never reached is therefore
# an infinite loop, so "did it finish" is a real assertion, not a formality.
reset_spies()
write_config()
win, result, finished = run_window()
R.check(finished, "a full simulated run terminates rather than hanging")
R.check(result.get("ok") is True, "the run reports success")

pvs = dict(app.DEFAULT_PVS)
written = [pv for pv, _v in WRITES]

# ═══ 2. exactly five PVs, and nothing else ═════════════════════════════════
ALLOWED = {pvs["auto_feedback"], pvs["feedback_v"], pvs["pitch"],
           pvs["roll"], pvs["mir_piezo_pitch"]}
extra = sorted({pv for pv in written} - ALLOWED)
R.check(not extra, "writes only the five permitted PVs (extra: %s)" % extra)

# ═══ 3. the DCM piezos are untouched, in every sense ═══════════════════════
piezo = {pvs["piezo_pitch"], pvs["piezo_roll"]}
R.check(not (piezo & set(written)), "never writes the DCM piezo PVs")
touched_reads = [r for r in READS if any(r.startswith(p) for p in piezo if p)]
R.check(not touched_reads,
        "never even reads a DCM piezo PV or field (got %s)" % touched_reads[:3])

# ═══ 4. horizontal feedback is read-only ═══════════════════════════════════
R.check(pvs["feedback_h"] not in written, "never writes H feedback")

# ═══ 5. vertical feedback is switched off and never re-enabled ═════════════
v_writes = [v for pv, v in WRITES if pv == pvs["feedback_v"]]
R.check(v_writes and all(float(v) == 0 for v in v_writes),
        "writes V feedback to 0 and never to 1 (got %s)" % v_writes)

# ═══ 6. the pitch motor is actually moved to the peak ══════════════════════
# _smart_scan_peak returns the peak but does not drive the motor; it leaves it
# at the last fine-scan point. Same for _scan_to_zero, which returns the
# interpolated crossing while the motor sits on the last stepped point. So the
# final write on each axis must equal the reported result, not merely exist.
# In simulation _smart_scan_peak synthesises the scan and emits points without
# stepping the motor, so the only pitch write is the explicit move to the peak.
# That single write is the whole property under test here.
pitch_writes = [v for pv, v in WRITES if pv == pvs["pitch"]]
R.check(len(pitch_writes) >= 1, "the pitch motor is written at all")
# The adaptive scan runs several passes, each its own trace, and the peak
# marker belongs to whichever pass produced the reported result -- so look for
# it across the figure rather than assuming it sits on the first trace.
marked = [s for s in win._models["mini_pitch"].order() if s.marker is not None]
R.check(len(marked) == 1,
        "exactly one pitch trace carries the peak marker (got %d)" % len(marked))
if marked and pitch_writes:
    R.check(abs(float(pitch_writes[-1]) - float(marked[0].marker)) < 1e-6,
            "the last pitch write equals the marked peak (%.6g vs %.6g)"
            % (pitch_writes[-1], marked[0].marker))

piezo_writes = [v for pv, v in WRITES if pv == pvs["mir_piezo_pitch"]]
zero = next((s for s in win._models["mini_piezo"].order()
             if s.marker is not None), None)
if zero is not None and piezo_writes:
    R.check(abs(float(piezo_writes[-1]) - float(zero.marker)) < 1e-6,
            "the last mirror-piezo write equals the marked zero crossing")

# ═══ 7. figures are monotonic in x where they should be ════════════════════
pf = win._models["mini_pitch"].get("m_pitch")
R.check(pf is not None and len(pf.xs) >= 2, "the pitch figure received points")
# The property that matters: a scan pass measures each x once, so within a
# single trace x must be strictly increasing. Duplicated x values are what
# made the old single-trace plot zigzag when a fine pass re-measured the
# region a coarse pass had already covered.
for figname in ("mini_pitch", "mini_roll", "mini_piezo"):
    for sr in win._models[figname].order():
        strictly_up = all(b > a for a, b in zip(sr.xs, sr.xs[1:]))
        R.check(strictly_up,
                "%s / %r: x is strictly increasing within the trace"
                % (figname, sr.label))

# ═══ 8. V-feedback state is announced ══════════════════════════════════════
logtext = win.log.toPlainText()
R.check("NOT engaged" in logtext or "not engaged" in logtext.lower(),
        "the log states that vertical feedback was left off")
R.check("lock-in" not in logtext.lower(),
        "the log does not claim 'lock-in' for a run that never closed the loop")
win.close()

# ═══ 9. the BPM x threshold decides whether roll moves at all ══════════════
for label, bpm_x, expect_roll in (("inside", 3.0, False), ("outside", 40.0, True)):
    reset_spies()
    write_config()
    SEED[app.DEFAULT_PVS["bpm_x"]] = bpm_x
    w, res, fin = run_window()
    rolled = app.DEFAULT_PVS["roll"] in [pv for pv, _ in WRITES]
    R.check(fin and res.get("ok") is True,
            "run completes with BPM x = %.0f um (%s threshold)" % (bpm_x, label))
    R.check(rolled is expect_roll,
            "BPM x = %.0f um %s threshold -> roll %s written"
            % (bpm_x, label, "is" if expect_roll else "is NOT"))
    rec = (w._history_rows or [{}])[-1]
    if not expect_roll:
        R.check(rec.get("roll_skipped_reason") == "within_threshold",
                "a threshold skip is recorded as within_threshold (got %r)"
                % rec.get("roll_skipped_reason"))
        R.check(rec.get("steps_skipped", {}).get("m_roll") == "within_threshold",
                "steps_skipped names the reason, not just the step")
    else:
        R.check(rec.get("roll_skipped_reason") in (None, ""),
                "a roll scan that ran records no skip reason (got %r)"
                % rec.get("roll_skipped_reason"))
        R.check(rec.get("dcm_roll_urad") is not None,
                "a roll scan that ran records the resulting roll position")
    R.check(rec.get("bpm_x_threshold_um") == 12.0,
            "the threshold in force is stamped in the record")
    R.check(rec.get("bpm_x_before_um") is not None,
            "the measured BPM x the decision was based on is recorded")
    R.check(rec.get("feedback_v_engaged") is False,
            "the record states V feedback was not engaged")
    w.close()

# ═══ 10. an operator skip is not the same as a threshold skip ══════════════
reset_spies()
write_config()
SEED[app.DEFAULT_PVS["bpm_x"]] = 40.0          # would otherwise scan
w, res, fin = run_window(steps=("m_pitch", "m_piezo"))
R.check(fin and res.get("ok") is True, "run completes with roll un-ticked")
R.check(app.DEFAULT_PVS["roll"] not in [pv for pv, _ in WRITES],
        "an un-ticked roll step writes nothing to roll")
rec = (w._history_rows or [{}])[-1]
R.check(rec.get("roll_skipped_reason") == "operator_unticked",
        "an operator skip is recorded as operator_unticked, not as a threshold "
        "skip (got %r)" % rec.get("roll_skipped_reason"))
R.check(rec.get("steps_skipped", {}).get("m_roll") == "operator_unticked",
        "steps_skipped distinguishes an operator skip from a threshold skip")
w.close()

# ═══ 11. the no-DCM-piezo case: blank piezo PVs must not break the run ═════
reset_spies()
write_config(piezo_pitch="", piezo_roll="")
w, res, fin = run_window()
R.check(fin and res.get("ok") is True,
        "the run succeeds with both DCM piezo PVs blank (the whole point)")
w.close()

# ═══ 12. a blank ion chamber refuses, and moves nothing ════════════════════
reset_spies()
write_config(ion_chamber="")
w = mini.MiniWindow()
w.show()
w.sim_chk.setChecked(True)
refused = {}
w.alignment_done.connect(lambda ok: refused.setdefault("ok", ok))
w._on_start()
H.pump(lambda: False, 400)          # give any thread a chance to start
R.check(not WRITES, "a blank ion_chamber PV refuses to start and writes nothing")
R.check("ok" not in refused, "no run is reported as finished when refused")
w.close()

# ═══ 13. the travel window refuses rather than clamps ══════════════════════
# A clamping ao record plus a scan that records demands rather than readbacks
# would put fictitious x values on the plot and in the record. So an
# out-of-window write must raise, not silently succeed at a different value.
reset_spies()
cfg = write_config()
wk = mini.MiniAlignmentWorker(cfg["pvs"], dict(app.DEFAULT_SCAN), simulate=True,
                              enabled={"m_voff", "m_pitch"})
wk._declare_window(cfg["pvs"]["pitch"], 1000.0, 0.5, "DCM pitch")
# _write returns None on success and raises on failure, so "succeeded" means
# "did not raise" and, more to the point, "reached the transport".
inside_ok = True
try:
    wk._write(cfg["pvs"]["pitch"], 1000.2, "test inside window")
except Exception as exc:
    inside_ok = False
    R.fail("a write inside the declared window raised: %r" % exc)
R.check(inside_ok and (cfg["pvs"]["pitch"], 1000.2) in WRITES,
        "a write inside the declared window reaches the transport")
raised = False
try:
    wk._write(cfg["pvs"]["pitch"], 1200.0, "test outside window")
except Exception as exc:
    raised = isinstance(exc, app.PVFaultAbort) or "window" in str(exc).lower()
R.check(raised, "a write outside the declared window raises instead of clamping")
outside = [v for pv, v in WRITES if pv == cfg["pvs"]["pitch"] and abs(v - 1200.0) < 1]
R.check(not outside, "the out-of-window value never reached the transport")

# ═══ 14. nothing moves before pre-flight passes ════════════════════════════
# Hardware mode with every probe failing: the run must stop with zero writes,
# in particular before V feedback is switched off.
reset_spies()
cfg = write_config()
_real_probe = app.probe_pv
app.probe_pv = lambda pv, timeout=2.0, try_rbv=False: (False, "stubbed failure")
_was_avail = app.EPICS_AVAILABLE
app.EPICS_AVAILABLE = True
try:
    wk = mini.MiniAlignmentWorker(cfg["pvs"], dict(app.DEFAULT_SCAN),
                                  simulate=False,
                                  enabled={"m_voff", "m_pitch", "m_piezo"})
    done = {}
    wk.finished.connect(lambda ok: done.setdefault("ok", ok))
    wk.pv_fault.connect(lambda *a: wk.fault_abort())
    wk.run()
    R.check(done.get("ok") is False, "a failing pre-flight ends the run unsuccessfully")
    R.check(not WRITES,
            "a failing pre-flight writes nothing at all (got %s)" % WRITES[:3])
finally:
    app.probe_pv = _real_probe
    app.EPICS_AVAILABLE = _was_avail

# ═══ 15. the mini config section persists and survives the full console ════
reset_spies()
write_config()
w, res, fin = run_window()
w.close()
saved = json.load(io.open(app.AUTO_CONFIG_PATH, encoding="utf-8"))
R.check("mini" in saved, "the run writes a `mini` section to the shared config")
R.check(isinstance(saved.get("mini", {}).get("records"), list)
        and saved["mini"]["records"],
        "the `mini` section carries at least one run record")
mini_before = json.loads(json.dumps(saved["mini"]))

full = app.MainWindow()
full._save_config()
full.close()
after = json.load(io.open(app.AUTO_CONFIG_PATH, encoding="utf-8"))
R.check(after.get("mini") == mini_before,
        "the `mini` section survives a full-console load-and-save cycle verbatim")
R.check("pvs" in after and "energy_table" in after,
        "the full console still wrote its own keys alongside it")

# ═══ 16. the main console's figure board is untouched ══════════════════════
R.check(len(app._FIGURE_DEFS) == 8,
        "the main app still declares exactly 8 figures (got %d)"
        % len(app._FIGURE_DEFS))
R.check(set(mini._MINI_SCAN_ROUTES) & set(app._SCAN_ROUTES) == set(),
        "the mini route table shares no substep key with the main one")

# ═══ 17. clean close, and no lock left behind ══════════════════════════════
R.check(app.read_run_lock() is None,
        "no run lock is left held after the runs complete")

app.EpicsInterface.get = _real_get
app.EpicsInterface.put = _real_put
R.finish()
