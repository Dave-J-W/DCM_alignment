# -*- coding: utf-8 -*-
"""Pre-flight speed and .RTYP classification tests. No IOC required.

Covers the item-11 rewrite of AlignmentWorker._preflight():
  - the connection-probe pass now runs concurrently in a ThreadPoolExecutor
    instead of one PV at a time, so a batch of slow/dead PVs no longer costs
    one full timeout each, serially;
  - motor/plain classification reads .RTYP (a dbCommon field every record
    type answers immediately) instead of timing out a .DMOV probe on every
    plain record, with a per-PV .DMOV fallback only when .RTYP itself does
    not answer;
  - none of the above breaks preflight_report's label ordering, or how
    promptly an abort during pre-flight is noticed.

Each scenario below replaces AlignmentWorker._required_pvs() on a bare
worker instance with a fixed, synthetic PV list, so these tests are isolated
from DEFAULT_PVS / mirror-stage config (not what item 11 is about) and the
PV counts and RTYP values are exactly what each assertion needs.

    py -3.9 tests/test_preflight_speed.py
"""
import threading
import time

import _harness as H

app = H.app
R = H.Report("DCM Alignment Console - pre-flight speed & .RTYP suite")

# ── stub the EPICS layer so this runs without hardware, as test_pv_faults.py does ──
app.EPICS_AVAILABLE = True
try:
    import epics
    epics.ca.use_initial_context = lambda *a, **k: None
except ImportError:
    pass

qapp = H.qapp()   # AlignmentWorker is a QObject; needs a QApplication to exist


def make_worker(required_pvs):
    """A bare AlignmentWorker with _required_pvs() replaced by a fixed list.

    required_pvs is a list of (label, name, try_rbv, writable) tuples, the
    same 4-tuple shape _required_pvs() itself returns (see dcm_align_app.py).
    """
    w = app.AlignmentWorker(dict(app.DEFAULT_PVS), dict(app.DEFAULT_SCAN),
                            dict(app.DEFAULT_LOOKUP[0]), simulate=False)
    w._required_pvs = lambda: list(required_pvs)
    return w


def rtyp_stub(rtyp_map):
    """An EpicsInterface.get replacement answering only PV + '.RTYP' lookups."""
    def get(pv, as_string=False, timeout=3.0):
        if pv.endswith(".RTYP"):
            return rtyp_map.get(pv[:-len(".RTYP")])
        return 0.0
    return get


# ═══ 1. The concurrent connection pass is fast where serial would not be ═══
N, SLEEP = 20, 0.2
required_speed = [("Speed %d" % i, "PVT:speed:%d" % i, False, False)
                   for i in range(N)]

speed_calls = {"connect": 0, "dmov": 0}


def slow_probe(pv_name, timeout=2.0, try_rbv=False):
    if pv_name.endswith(".DMOV"):
        speed_calls["dmov"] += 1
    else:
        speed_calls["connect"] += 1
    time.sleep(SLEEP)
    return True, "ok"


app.probe_pv = slow_probe

w_speed = make_worker(required_speed)
t0 = time.time()
w_speed._preflight()
elapsed = time.time() - t0
serial_estimate = N * SLEEP

R.check(elapsed < 2.0,
        "concurrent pre-flight over %d PVs at %.1fs each finished in %.3fs "
        "-- well under the >= %.1fs a serial pass would take"
        % (N, SLEEP, elapsed, serial_estimate))
R.check(speed_calls["connect"] == N,
        "every required PV was probed exactly once (%d calls for %d PVs)"
        % (speed_calls["connect"], N))
R.check(speed_calls["dmov"] == 0,
        "no writable PVs in this scenario, so no .DMOV probe fired (%d)"
        % speed_calls["dmov"])

# ═══ 2. .RTYP alone classifies -- zero .DMOV probes when every PV answers ══
required_rtyp_ok = [
    ("Motor A", "PVT:mtr:A",  False, True),
    ("AO B",    "PVT:ao:B",   False, True),
    ("Calc E",  "PVT:calc:E", False, True),
]
RTYP_OK = {"PVT:mtr:A": "motor", "PVT:ao:B": "ao", "PVT:calc:E": "calc"}
dmov_calls_ok = []


def probe_ok(pv_name, timeout=2.0, try_rbv=False):
    if pv_name.endswith(".DMOV"):
        dmov_calls_ok.append(pv_name)
    return True, "ok"


app.probe_pv = probe_ok

w_ok = make_worker(required_rtyp_ok)
w_ok.epics.get = rtyp_stub(RTYP_OK)
rows_ok = []
w_ok.preflight_report.connect(lambda rows: rows_ok.append(rows))
w_ok._preflight()

R.check(len(dmov_calls_ok) == 0,
        ".RTYP alone classified every writable PV -- zero .DMOV probes were "
        "made (%d)" % len(dmov_calls_ok))
R.check(w_ok._is_motor.get("PVT:mtr:A") is True
        and w_ok._is_motor.get("PVT:ao:B") is False
        and w_ok._is_motor.get("PVT:calc:E") is False,
        "only RTYP == 'motor' classifies as a motor: %s" % w_ok._is_motor)
R.check(w_ok._rtype == RTYP_OK,
        "self._rtype is populated with the raw .RTYP string per PV: %s"
        % w_ok._rtype)

# rows must stay in label order: connection rows in _required_pvs() order,
# then classification rows for the writable subset, same relative order.
assert rows_ok, "preflight_report never fired"
got_labels = [r[0] for r in rows_ok[0]]
want_labels = ([lbl for lbl, _n, _r, _w in required_rtyp_ok]
               + [lbl + "  → type" for lbl, _n, _r, w in required_rtyp_ok if w])
R.check(got_labels == want_labels,
        "preflight_report rows stay in label order (got %s)" % got_labels)

# ═══ 3. A PV whose .RTYP does not answer falls back to .DMOV, alone ════════
required_fallback = [
    ("Motor A",   "PVT:mtr:A",  False, True),
    ("Unknown C", "PVT:unk:C",  False, True),   # RTYP absent -> None
    ("AO B",      "PVT:ao:B",   False, True),
]
RTYP_PARTIAL = {"PVT:mtr:A": "motor", "PVT:ao:B": "ao"}   # unk:C deliberately absent
dmov_calls_fb = []


def probe_fallback(pv_name, timeout=2.0, try_rbv=False):
    if pv_name.endswith(".DMOV"):
        dmov_calls_fb.append(pv_name)
        # The record behind "Unknown C" turns out to be a motor after all --
        # exactly the "fronted by a non-'motor' record" case the fallback
        # exists for.
        return pv_name.startswith("PVT:unk:C"), "ok"
    return True, "ok"


app.probe_pv = probe_fallback

w_fb = make_worker(required_fallback)
w_fb.epics.get = rtyp_stub(RTYP_PARTIAL)
w_fb._preflight()

R.check(dmov_calls_fb == ["PVT:unk:C.DMOV"],
        "the .DMOV fallback fired only for the one PV whose .RTYP did not "
        "answer (%s)" % dmov_calls_fb)
R.check(w_fb._is_motor.get("PVT:unk:C") is True,
        "the fallback probe result is honoured (RTYP-less PV classified as motor)")
R.check(w_fb._is_motor.get("PVT:mtr:A") is True
        and w_fb._is_motor.get("PVT:ao:B") is False,
        "the two PVs with a real .RTYP were not affected by the fallback: %s"
        % w_fb._is_motor)
R.check(w_fb._rtype.get("PVT:unk:C") is None
        and w_fb._rtype.get("PVT:mtr:A") == "motor",
        "self._rtype records None for the PV .RTYP could not answer, and the "
        "real value for the others: %s" % w_fb._rtype)

# ═══ 4. Abort during pre-flight still raises promptly ══════════════════════
ABORT_SLEEP = 2.0
required_abort = [("Abort %d" % i, "PVT:abort:%d" % i, False, False)
                   for i in range(5)]
abort_probe_calls = {"n": 0}


def hang_probe(pv_name, timeout=2.0, try_rbv=False):
    abort_probe_calls["n"] += 1
    time.sleep(ABORT_SLEEP)   # much longer than the poll interval below
    return True, "ok"


app.probe_pv = hang_probe

w_abort = make_worker(required_abort)
outcome = {}


def run_preflight():
    try:
        w_abort._preflight()
        outcome["result"] = "returned"
    except app.PVFaultAbort:
        outcome["result"] = "aborted"
    except Exception as exc:
        outcome["result"] = "error: %r" % exc


th = threading.Thread(target=run_preflight, daemon=True)
t_abort0 = time.time()
th.start()
time.sleep(0.3)          # let the pool actually start the (2.0s) probes
w_abort._abort = True    # simulate the operator hitting Abort
th.join(timeout=5.0)
abort_elapsed = time.time() - t_abort0

R.check(not th.is_alive(),
        "the pre-flight call returned after abort rather than hanging")
R.check(outcome.get("result") == "aborted",
        "abort during pre-flight raises PVFaultAbort (got %r)"
        % outcome.get("result"))
R.check(abort_elapsed < 1.5,
        "abort was noticed and raised promptly: %.3fs (probes sleep %.1fs "
        "each and were still in flight)" % (abort_elapsed, ABORT_SLEEP))

R.finish()
