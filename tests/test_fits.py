# -*- coding: utf-8 -*-
"""The fit models, and the traces they are drawn on.

Two properties are under test here.

First, the fits themselves: an intensity scan is a super-Gaussian and a BPM
zero-crossing scan is an error function, and each fit has to recover
parameters it was given, survive noise, and return None rather than nonsense
when handed something it cannot fit. A fit that silently returns a plausible
wrong answer is worse than no fit, because _scan_to_zero now moves a motor to
the fitted centre.

Second, the traces: a scan pass measures each x once, so x must be strictly
increasing within a trace. The adaptive peak scan runs several passes over
overlapping x, and when they all shared one trace the re-measured points
interleaved and drew a zigzag.

    py -3.9 tests/test_fits.py
"""
import numpy as np

import _harness as H

app = H.app
R = H.Report("DCM Alignment — fit models and scan traces")

# ═══ the erf model and its fit ═════════════════════════════════════════════
has_erf = hasattr(app, "erf_step") and hasattr(app, "fit_erf")
R.check(has_erf, "erf_step() and fit_erf() exist")

if has_erf:
    # A BPM zero-crossing scan: an erf rising through zero at a known centre.
    TRUE_C, TRUE_W, TRUE_A, TRUE_O = 1337.5, 0.02, 40.0, 0.0
    xs = np.linspace(TRUE_C - 0.08, TRUE_C + 0.08, 31)
    clean = app.erf_step(xs, TRUE_A, TRUE_C, TRUE_W, TRUE_O)

    R.check(abs(float(clean[0]) + TRUE_A) < TRUE_A * 0.2
            and abs(float(clean[-1]) - TRUE_A) < TRUE_A * 0.2,
            "erf_step saturates to -amplitude and +amplitude across the range")
    mid = float(app.erf_step(np.array([TRUE_C]), TRUE_A, TRUE_C, TRUE_W, TRUE_O)[0])
    R.check(abs(mid - TRUE_O) < 1e-9, "erf_step equals the offset at its centre")

    popt = app.fit_erf(xs, clean)
    R.check(popt is not None, "fit_erf converges on noiseless erf data")
    if popt is not None:
        R.check(abs(popt[1] - TRUE_C) < 1e-3,
                "fit_erf recovers the centre on clean data (%.5f vs %.5f)"
                % (popt[1], TRUE_C))

    rng = np.random.default_rng(20260918)
    noisy = clean + rng.normal(0, TRUE_A * 0.05, xs.size)
    popt_n = app.fit_erf(xs, noisy)
    R.check(popt_n is not None, "fit_erf converges with 5% noise")
    if popt_n is not None:
        err = abs(popt_n[1] - TRUE_C)
        R.check(err < TRUE_W,
                "fit_erf recovers the centre to better than one width under "
                "noise (err %.5f, width %.5f)" % (err, TRUE_W))
        # The whole reason to fit rather than interpolate between the two
        # points that straddle zero: the fit should not be worse than that.
        lin = app.find_zero_crossing(list(xs), list(noisy))
        R.check(err <= abs(lin - TRUE_C) * 1.5 + 1e-9,
                "the erf fit is no worse than linear interpolation "
                "(fit %.5f vs interp %.5f)" % (err, abs(lin - TRUE_C)))

    R.check(app.fit_erf([0.0, 1.0], [0.0, 1.0]) is None,
            "fit_erf returns None on too few points rather than guessing")

# ═══ the super-Gaussian, used for ion-chamber peaks ════════════════════════
TRUE_PK, TRUE_SIG, TRUE_AMP = -0.0125, 0.015, 1000.0
xs_p = np.linspace(TRUE_PK - 0.06, TRUE_PK + 0.06, 41)
ys_p = app.gaussian(xs_p, TRUE_PK, TRUE_SIG, TRUE_AMP, 10.0)
sg = app.fit_super_gaussian(xs_p, ys_p)
R.check(sg is not None, "fit_super_gaussian converges on a clean Gaussian")
if sg is not None:
    R.check(abs(sg[1] - TRUE_PK) < TRUE_SIG * 0.1,
            "fit_super_gaussian recovers the peak centre (%.5f vs %.5f)"
            % (sg[1], TRUE_PK))
R.check(app.fit_super_gaussian([0.0, 1.0, 2.0], [1.0, 1.0, 1.0]) is None,
        "fit_super_gaussian returns None on too few points")

# ═══ ScanSeries carries its fit, and round-trips it ════════════════════════
sr = app.ScanSeries("3_3b", "3B pitch coarse", "#1f77b4", "peak")
R.check(hasattr(sr, "fit_xs") and hasattr(sr, "fit_ys"),
        "ScanSeries has fit_xs / fit_ys")
R.check(list(sr.fit_xs) == [] and list(sr.fit_ys) == [],
        "a fresh series starts with no fit")
for x, y in ((1.0, 10.0), (2.0, 20.0), (3.0, 15.0)):
    sr.add_point(x, y)
sr.set_fit([1.0, 2.0, 3.0], [9.5, 20.5, 14.5])
R.check(list(sr.fit_xs) == [1.0, 2.0, 3.0], "set_fit stores the curve")
rt = app.ScanSeries.from_dict(sr.to_dict())
R.check(list(rt.fit_xs) == list(sr.fit_xs) and list(rt.fit_ys) == list(sr.fit_ys),
        "the fit survives a to_dict / from_dict round trip")

# ═══ FigureModel.set_fit announces itself ══════════════════════════════════
fm = app.FigureModel("t", "T", "T", "T", "x", "y")
fm.series("3_3b", "3B", "peak")
seen = []
fm.fit_changed.connect(seen.append)
fm.set_fit("3_3b", [1.0, 2.0], [3.0, 4.0])
R.check(seen == ["3_3b"], "FigureModel.set_fit emits fit_changed (got %r)" % seen)
R.check(list(fm.get("3_3b").fit_xs) == [1.0, 2.0],
        "FigureModel.set_fit reaches the series")

# ═══ traces: one x per point, per pass ═════════════════════════════════════
# Drive the real adaptive scan in simulation and inspect what it drew.
H.silence_dialogs()
qapp = H.qapp()

win = app.MainWindow()
win.show()
win.setup_tab.sim_check.setChecked(True)
win.energy_tab.table.selectRow(0)
win.alignment_tab.confirm_chk.setChecked(False)
result = {}
win.alignment_tab.alignment_done.connect(lambda ok: result.setdefault("ok", ok))
# Chapter 3 only: that is where the multi-pass peak scan lives.
win._start_alignment(enabled={"1_1a", "3_3a", "3_3b", "3_3c", "3_3d"})
ran = H.pump(lambda: "ok" in result, 180000)
R.check(ran, "the chapter-3 simulated run finishes")

board = win.alignment_tab._plot_board
pitch_fig = board.model("dcm_pitch")
series = pitch_fig.order()
R.check(len(series) >= 2,
        "the pitch figure carries a trace per scan pass (got %d)" % len(series))

bad = []
for m_id in ("dcm_pitch", "dcm_roll"):
    for s in board.model(m_id).order():
        if not all(b > a for a, b in zip(s.xs, s.xs[1:])):
            dupes = len(s.xs) - len(set(s.xs))
            bad.append("%s/%s (%d duplicate x)" % (m_id, s.label, dupes))
R.check(not bad, "x is strictly increasing within every trace (offenders: %s)" % bad)

labels = [s.label for s in series]
R.check(len(labels) == len(set(labels)),
        "every trace has a distinct legend entry (%r)" % labels)
colors = [s.color for s in series]
R.check(len(colors) == len(set(colors)) or len(colors) > len(app.SERIES_COLORS),
        "traces are distinctly coloured")

fitted = [s for s in series if len(s.fit_xs) > 0]
R.check(fitted, "at least one pitch trace carries a plotted fit curve")
roll_fitted = [s for s in board.model("dcm_roll").order() if len(s.fit_xs) > 0]
R.check(roll_fitted, "the roll (BPM zero) scan carries a plotted fit curve")

win.close()
R.finish()
