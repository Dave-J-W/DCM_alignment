# -*- coding: utf-8 -*-
"""PV fault-path tests against a stubbed EPICS layer. No IOC required.

Covers pre-flight blocking before anything moves, the fault dialog contents,
Check PV, dismiss-keeps-paused, Try Again resuming, and Abort stopping cleanly.

    py -3.9 tests/test_pv_faults.py
"""
import _harness as H

app = H.app
R = H.Report("DCM Alignment Console - PV fault suite")

H.silence_dialogs(question=H.QMessageBox.StandardButton.No)

# ── stub the EPICS layer so the fault paths run without hardware ────────────
app.EPICS_AVAILABLE = True
try:
    import epics
    epics.ca.use_initial_context = lambda *a, **k: None
except ImportError:
    pass

STATE = {"probe_ok": False, "get_ok": True, "puts": [], "described": [],
         "scan_pv": None, "scan_i": 0}

app.probe_pv = lambda pv, timeout=2.0, try_rbv=False: (
    (True, "ok") if STATE["probe_ok"] else (False, "timeout"))
app.describe_pv = lambda pv, timeout=2.0: (
    STATE["described"].append(pv) or "stub diagnostic for %s: NOT CONNECTED" % pv)


# A settled motor record: done, on target, no problem bits.
_SETTLED = ((".DMOV", 1.0), (".MOVN", 0.0), (".MSTA", 2.0), (".LVIO", 0.0),
            (".MISS", 0.0), (".DIFF", 0.0), (".RDBD", 0.01), (".MRES", 0.001))
_IDLE_PVS = {app.DEFAULT_PVS["und_busy"]: 0.0,          # undulator not moving
             app.DEFAULT_PVS["shutter_blocking"]: 0.0}  # nothing blocking the beam


_BPM_PVS = {app.DEFAULT_PVS["bpm_x"], app.DEFAULT_PVS["bpm_y"]}

# Pre-flight now classifies motor vs plain by .RTYP (item 11) instead of timing
# out a .DMOV probe, so this stub needs an .RTYP answer for every writable PV
# pre-flight can ask about, or the bare "return 0.5" fallback below would hand
# back a float where AlignmentWorker._preflight() expects a string/None and
# crash on .strip(). MOTOR_PV_KEYS is the app's own existing "these PVs are
# EPICS motor records" set (used for .RBV retry in SetupTab), so it is reused
# here rather than inventing a second list that could drift from it.
_MOTOR_PVS = {app.DEFAULT_PVS[k] for k in app.MOTOR_PV_KEYS if app.DEFAULT_PVS.get(k)}


def fake_get(self, pv, as_string=False, timeout=3.0):
    if self.simulate:
        return self._sim_vals.get(pv, 0.0)
    if not STATE["get_ok"]:
        return None
    if pv.endswith(".RTYP"):
        base = pv[:-len(".RTYP")]
        return "motor" if base in _MOTOR_PVS else "ao"
    if pv in _IDLE_PVS:
        return _IDLE_PVS[pv]
    for suffix, val in _SETTLED:
        if pv.endswith(suffix):
            return val
    if pv in _BPM_PVS:
        # A ramp that crosses zero on the third point of whichever scan is
        # running. _scan_to_zero now faults on a signal that never crosses, so
        # a constant here would (correctly) stop every zero-crossing scan.
        return float(3 - STATE["scan_i"])
    return 0.5


def fake_put(self, pv, value, wait=True, timeout=30.0):
    if self.simulate:
        self._sim_vals[pv] = value
        return True, ""
    STATE["puts"].append((pv, value))
    if pv == STATE["scan_pv"]:
        STATE["scan_i"] += 1
    else:                       # a different PV: a new scan has begun
        STATE["scan_pv"], STATE["scan_i"] = pv, 0
    return True, ""


app.EpicsInterface.get = fake_get
app.EpicsInterface.put = fake_put

qapp = H.qapp()
win = app.MainWindow()
win.show()
tab = win.alignment_tab
win.setup_tab.sim_check.setChecked(False)   # hardware mode, but stubbed
win.energy_tab.table.selectRow(0)
tab.set_chapter_enabled(4, False)          # chapter 4 off, as before
tab.confirm_chk.setChecked(False)
# Keep the stubbed run short so a hang is obvious rather than just slow.
for key, val in [("pitch_steps", 7), ("roll_steps", 7), ("dcm_piezo_steps", 5),
                 ("mir_piezo_steps", 5), ("smart_max_extend_steps", 2),
                 ("smart_fine_scan_iter", 1), ("settle_time", 0.0),
                 ("piezo_settle_time", 0.0)]:
    win.set_scan_param(key, val)

# ═══ 1. Pre-flight blocks the run before anything moves ════════════════════
STATE["probe_ok"] = False
STATE["puts"] = []
win._start_alignment()

if not R.check(H.pump(lambda: tab._fault_dlg is not None),
               "a pre-flight failure opens the fault dialog"):
    R.finish()

R.check(tab._faulted, "the tab records the faulted state")
R.check(STATE["puts"] == [],
        "nothing was written to any PV before the pre-flight fault (%d writes)"
        % len(STATE["puts"]))
R.check(tab.fault_banner.isVisible(), "the paused banner is shown")
R.check("PAUSED" in tab.fault_tag.text(), "the banner reads PAUSED")
R.check(len(tab._fault_rows) > 1,
        "every failing PV is listed, not just the first (%d rows)" % len(tab._fault_rows))

# Check PV must diagnose without resuming.
dlg = tab._fault_dlg
dlg._on_check()
H.pump(lambda: "NOT CONNECTED" in dlg._diag.toPlainText())
R.check(bool(STATE["described"]), "Check PV probes the selected PV")
R.check("NOT CONNECTED" in dlg._diag.toPlainText(), "the Check PV result is displayed")
R.check(dlg.retry_btn.isEnabled() and dlg.abort_btn.isEnabled(),
        "Check PV leaves the run paused, with Try Again and Abort still offered")

# Dismissing the dialog must not resume the sequence.
dlg.reject()
H.pump(lambda: tab._fault_dlg is None, 2000)
R.check(tab._fault_dlg is None, "dismissing the dialog closes it")
R.check(tab._worker is not None and tab._worker._paused,
        "the worker stays paused after the dialog is dismissed")
R.check(tab._running, "the run is still active, paused rather than finished")
R.check(tab.fault_btn.isVisible(), "a Review fault button is offered to reopen it")

tab._open_fault_dialog()
R.check(tab._fault_dlg is not None, "Review fault reopens the dialog")

# Try Again with the PVs healthy must resume and finish.
STATE["probe_ok"] = True
done = {}
tab.alignment_done.connect(lambda ok: done.setdefault("ok", ok))
tab._fault_dlg._on_retry()
R.check(H.pump(lambda: "ok" in done, 120000) and done.get("ok") is True,
        "Try Again resumes and the sequence completes once the PVs answer")
R.check(len(STATE["puts"]) > 0, "motors were driven only after the fault cleared")

# ═══ 2. Abort at a mid-sequence read fault ═════════════════════════════════
STATE["probe_ok"] = True    # pre-flight passes
STATE["get_ok"] = False     # every later read returns None
STATE["puts"] = []
done2 = {}
tab.alignment_done.connect(lambda ok: done2.setdefault("ok", ok))
win._start_alignment()

if R.check(H.pump(lambda: tab._fault_dlg is not None),
           "a mid-sequence None read raises a fault"):
    pv, context, reason, _when = tab._fault_rows[-1]
    R.check("None" in reason or "disconnected" in reason,
            "the fault names the disconnect: %r" % (reason,))
    R.check(bool(context), "the fault records where it broke: %r" % (context,))
    tab._fault_dlg._on_abort()
    R.check(H.pump(lambda: "ok" in done2, 60000) and done2.get("ok") is False,
            "Abort stops the sequence")
    R.check(not tab._running, "the run is no longer active after abort")
    R.check(not tab.fault_banner.isVisible(), "the banner clears after abort")
    R.check(any(i["tag"].text() == "Error" for i in tab._step_row_info.values()),
            "the interrupted step is marked Error rather than left Running")

# ═══ 3. Motion completion: no wall-clock cap, but stalls are caught ════════
# Exercised directly on a worker rather than through a whole sequence: these
# are timing properties of _wait_motor_done, and a real run would take minutes.
import time

MOTOR = "FAKE:m1"


def make_worker(stall=2.0, grace=2.0):
    params = dict(app.DEFAULT_SCAN)
    params.update({"motor_stall_timeout": stall, "motor_start_grace": grace})
    w = app.AlignmentWorker(dict(app.DEFAULT_PVS), params,
                            dict(app.DEFAULT_LOOKUP[0]), simulate=False)
    w._is_motor[MOTOR] = True          # skip the .DMOV probe
    return w


def drive(w, dmov_fn, rbv_fn, diff=0.0):
    """Point the worker's transport at a scripted motor."""
    def get(pv, as_string=False, timeout=3.0):
        if pv.endswith(".DMOV"):
            return dmov_fn()
        if pv.endswith(".RBV"):
            return rbv_fn()
        if pv.endswith(".RDBD"):
            return 0.01
        if pv.endswith(".MRES"):
            return 0.001
        if pv.endswith(".DIFF"):
            return diff
        return 0.0                     # MSTA, LVIO, MISS all clean
    w.epics.get = get


# -- a slow but healthy motor must NOT fault, even past the stall timeout ----
STALL, MOVING_FOR = 2.0, 5.0           # move lasts 2.5x the stall timeout
w = make_worker(stall=STALL)
t0 = time.time()
drive(w,
      dmov_fn=lambda: 0.0 if time.time() - t0 < MOVING_FOR else 1.0,
      rbv_fn=lambda: (time.time() - t0) * 1.0)      # advancing steadily
faults = []
w.pv_fault.connect(lambda pv, c, r: faults.append((pv, r)))
started = time.time()
ok = w._wait_motor_done(MOTOR)
elapsed = time.time() - started
R.check(ok is True and not faults,
        "a %.0fs move with a %.0fs stall timeout does not fault while .RBV advances"
        % (MOVING_FOR, STALL))
R.check(elapsed >= MOVING_FOR,
        "the wait lasted the whole move rather than timing out (%.1fs)" % elapsed)

# -- a motor that reports moving but stops advancing IS caught --------------
w2 = make_worker(stall=1.5)
drive(w2, dmov_fn=lambda: 0.0, rbv_fn=lambda: 42.0)  # frozen encoder
caught = []
w2.pv_fault.connect(lambda pv, c, r: (caught.append((pv, c, r)), w2.fault_abort()))
started = time.time()
try:
    w2._wait_motor_done(MOTOR)
    raised = False
except app.PVFaultAbort:
    raised = True
elapsed = time.time() - started
R.check(raised, "a frozen encoder while .DMOV says moving raises a fault")
if caught:
    pv, _ctx, reason = caught[0]
    R.check(pv == MOTOR, "the stall fault names the motor: %r" % pv)
    R.check("has not advanced" in reason, "the reason describes the stall: %r" % reason)
R.check(1.4 <= elapsed <= 5.0,
        "the stall was caught close to the configured timeout (%.1fs)" % elapsed)

# -- a setpoint the motor never acts on is caught by the start grace --------
w3 = make_worker(grace=1.0)
drive(w3, dmov_fn=lambda: 1.0, rbv_fn=lambda: 0.0, diff=5.0)  # done, but off target
caught3 = []
w3.pv_fault.connect(lambda pv, c, r: (caught3.append(r), w3.fault_abort()))
try:
    w3._wait_motor_done(MOTOR)
    raised3 = False
except app.PVFaultAbort:
    raised3 = True
R.check(raised3 and caught3 and "has not started" in caught3[0],
        "a setpoint the motor never acts on is caught by the start grace: %r"
        % (caught3[0] if caught3 else None))

# -- a PV an enabled step needs, but which is blank, must reach pre-flight ---
# It used to be omitted from _required_pvs entirely when empty, so the step just
# vanished at run time with a log line instead of blocking the run.
blank_pvs = dict(app.DEFAULT_PVS)
blank_pvs["mir_piezo_pitch"] = ""
w4 = app.AlignmentWorker(blank_pvs, dict(app.DEFAULT_SCAN),
                         dict(app.DEFAULT_LOOKUP[0]), simulate=False,
                         skip_mirror=False)
entries = w4._required_pvs()
flagged = [lbl for lbl, name, _rbv, _w in entries
           if "piezo pitch" in lbl.lower() and not name]
R.check(bool(flagged),
        "a blank mirror piezo PV is reported to pre-flight, not skipped: %s" % flagged)

# ...and it disappears again once the step that needs it is switched off.
w5 = app.AlignmentWorker(blank_pvs, dict(app.DEFAULT_SCAN),
                         dict(app.DEFAULT_LOOKUP[0]), simulate=False,
                         skip_mirror=True,
                         enabled={"1_1a"})
still = [lbl for lbl, name, _rbv, _w in w5._required_pvs()
         if "piezo pitch" in lbl.lower() and not name and "mirror" in lbl.lower()]
R.check(not still,
        "pre-flight does not demand a PV no enabled step uses: %s" % still)

# -- a closed upstream shutter stops a scan and waits for the operator -------
# The PSS owns the shutter, so unlike the feedback loops this must not be
# "corrected" by the app; it pauses until someone opens it and hits Try Again.
SHUTTER = app.DEFAULT_PVS["shutter_blocking"]
w6 = app.AlignmentWorker(dict(app.DEFAULT_PVS), dict(app.DEFAULT_SCAN),
                         dict(app.DEFAULT_LOOKUP[0]), simulate=False)
shut = {"blocking": 1.0}
w6.epics.get = (lambda pv, as_string=False, timeout=3.0:
                ("ON" if as_string else shut["blocking"]) if pv == SHUTTER else 0.0)
seen6 = []
w6.pv_fault.connect(lambda pv, c, r: (seen6.append((pv, r)), w6.fault_abort()))
try:
    w6._require_beam("3_3b")
    raised6 = False
except app.PVFaultAbort:
    raised6 = True
R.check(raised6, "a blocking upstream shutter stops the scan")
R.check(seen6 and seen6[0][0] == SHUTTER,
        "the shutter fault names the shutter PV: %s" % (seen6[0][0] if seen6 else None))
R.check(seen6 and "blocking the beam" in seen6[0][1] and "'ON'" in seen6[0][1],
        "the reason reports the shutter state: %r" % (seen6[0][1] if seen6 else None))

# ...and it lets the scan through once the shutter is open.
w7 = app.AlignmentWorker(dict(app.DEFAULT_PVS), dict(app.DEFAULT_SCAN),
                         dict(app.DEFAULT_LOOKUP[0]), simulate=False)
w7.epics.get = lambda pv, as_string=False, timeout=3.0: 0.0
faults7 = []
w7.pv_fault.connect(lambda pv, c, r: faults7.append(r))
w7._require_beam("3_3b")
R.check(not faults7, "an open shutter raises nothing")

R.finish()
