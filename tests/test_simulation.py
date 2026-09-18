# -*- coding: utf-8 -*-
"""Headless simulation tests. No EPICS connection required.

Runs the full alignment sequence in every skip-mirror / confirm combination and
checks the behaviours that have regressed before.

    py -3.9 tests/test_simulation.py
"""
import _harness as H

app = H.app
R = H.Report("DCM Alignment Console - simulation suite")

H.silence_dialogs()
qapp = H.qapp()
win = app.MainWindow()
win.show()

# mir_piezo_pitch used to ship blank, which silently skipped 4B2 and 5C on a
# fresh config -- the run "succeeded" with two steps quietly missing. Every PV a
# scan drives must have a real default.
_blank = [k for k in ("mir_piezo_pitch", "mir_pitch_motor", "piezo_pitch",
                      "piezo_roll", "bpm_x", "bpm_y", "mir_slit_top", "mir_slit_bot")
          if not (app.DEFAULT_PVS.get(k) or "").strip()]
R.check(not _blank, "no PV a scan drives ships blank%s"
        % (" (blank: %s)" % _blank if _blank else ""))


def run_sequence(skip_mirror, confirm):
    """Run one full sequence to completion. Returns the success flag, or None on timeout."""
    win.alignment_tab.set_chapter_enabled(4, not skip_mirror)
    win.alignment_tab.confirm_chk.setChecked(confirm)
    win.setup_tab.sim_check.setChecked(True)
    win.energy_tab.table.selectRow(0)

    result = {}
    win.alignment_tab.alignment_done.connect(lambda ok: result.setdefault("ok", ok))
    win._start_alignment()
    # With "Confirm each step" on, nothing advances until Proceed is clicked.
    win.alignment_tab._worker.confirm_needed.connect(
        lambda _key: H.QTimer.singleShot(10, win.alignment_tab._proceed_clicked))

    if not H.pump(lambda: "ok" in result, 180000):
        return None
    return result.get("ok")


for skip, conf in [(True, False), (False, False), (True, True)]:
    tag = "skip_mirror=%s confirm=%s" % (skip, conf)
    ok = run_sequence(skip, conf)
    if ok is None:
        R.fail("TIMED OUT: " + tag)
    else:
        R.check(ok, "full sequence completes (%s)" % tag)

# A blank stage PV makes _mirror_yz_pvs() hand back an empty VDM:Y name. In
# simulation _fault() returns "retry", so an unguarded blank name would spin the
# checked-read retry loop forever.
saved_stages = win.mirror_tab.get_mirror_stages()
blanked = [dict(st, pv="") if "VDM" in st.get("name", "") else st
           for st in saved_stages]
win.mirror_tab.apply_config({"mirror_stages": blanked})
ok = run_sequence(False, False)
R.check(bool(ok), "a blank VDM stage PV does not hang simulation")
win.mirror_tab.apply_config({"mirror_stages": saved_stages})

# get_checked_pvs() used to drop the "source" key, so every computed scan result
# failed the `if not pv: continue` test and never reached the lookup table.
records = win.energy_tab.get_record_data()
if not R.check(bool(records), "an alignment writes a lookup-table row"):
    pass
else:
    want = ["BPM Max Intensity w/o Mirror", "MonP Max Intensity w/o Mirror",
            "Mirror Stripe", "BPM Y @ 3D (µm)",
            "VDM Y FWHM @ 4D (µm)", "VFM Y FWHM @ 4E (µm)"]
    missing = [w for w in want if w not in records[-1]]
    R.check(not missing,
            "lookup row carries all six computed scan results%s"
            % (" (missing %s)" % missing if missing else ""))

# The JJC stays open at 4 for the whole alignment and is closed to its operating
# size only in Step 5, immediately before the feedback loops are enabled -- in
# both the Step-4-ran and Step-4-skipped branches.
WRITES = []
_real_put = app.EpicsInterface.put


def recording_put(self, pv, value, wait=True, timeout=30.0):
    WRITES.append((pv, value))
    return _real_put(self, pv, value, wait=wait, timeout=timeout)


app.EpicsInterface.put = recording_put
jjc_pv = next(st["pv"] for st in win.mirror_tab.get_mirror_stages()
              if "JJC" in st["name"] and "Size" in st["name"])
fb_h = win.get_pvs()["feedback_h"]
target = win.mirror_tab.get_mirror_scan_params()["jjc_size_pre_feedback"]

for skip in (True, False):
    WRITES[:] = []
    tag = "skip_mirror=%s" % skip
    if not run_sequence(skip, False):
        R.fail("JJC ordering: sequence did not complete (%s)" % tag)
        continue
    jjc_all = [(i, v) for i, (pv, v) in enumerate(WRITES) if pv == jjc_pv]
    closes = [i for i, v in jjc_all if abs(float(v) - target) < 1e-9]
    fb_on = [i for i, (pv, v) in enumerate(WRITES) if pv == fb_h and float(v) == 1.0]
    if not closes:
        R.fail("JJC never closed to %s (%s); writes were %s" % (target, tag, jjc_all))
    elif not fb_on:
        R.fail("H feedback was never enabled (%s)" % tag)
    elif closes[0] > fb_on[0]:
        R.fail("JJC closed AFTER feedback was enabled (%s)" % tag)
    else:
        early = [v for i, v in jjc_all if i < closes[0]]
        if any(abs(float(v) - target) < 1e-9 for v in early):
            R.fail("JJC reached %s before the final close (%s)" % (target, tag))
        else:
            R.check(True, "JJC closed to %s before feedback, open (%s) until then (%s)"
                    % (target, ", ".join(str(v) for v in early) or "no earlier writes", tag))

app.EpicsInterface.put = _real_put

# ── Figure routing. The loop above ended on a skip_mirror=False run, so every
#    figure should be populated. "pitch" used to be one key shared by the DCM
#    pitch motor and the DCM pitch piezo, concatenating incompatible x scales
#    into one polyline.
board = win.alignment_tab._plot_board

# dcm_pitch overlays 3B+3D, and each of those is now drawn as one trace per
# scan PASS (AlignmentWorker._smart_scan_peak): the coarse scan, then however
# many fine re-measurements it took to converge. The exact pass count is not
# pinned down -- it depends on how quickly the fit converges -- but every
# trace on the figure must trace back to one of the two substeps that feed
# it, and splitting into passes must produce MORE traces than the old
# one-trace-per-substep behaviour, not the same or fewer.
dcm_pitch_series = board.model("dcm_pitch").order()
dcm_pitch_bases = sorted({sr.key.split("#", 1)[0] for sr in dcm_pitch_series})
R.check(dcm_pitch_bases == ["3_3b", "3_3d"] and len(dcm_pitch_series) > 2,
        "figure 'dcm_pitch' carries one trace per scan pass of 3B and 3D "
        "(%d trace(s): %s)" % (len(dcm_pitch_series), [sr.key for sr in dcm_pitch_series]))

# mir_pitch (4C, µrad motor) and mir_piezo (5C, DCOM piezo) are single-pass
# _scan_to_zero scans -- never split into passes -- and deliberately separate
# figures: since 4C moved to the pitch motor they no longer share an x
# quantity and must not share an axis.
for fig_id, expected in (("mir_pitch", 1), ("mir_piezo", 1)):
    got = len(board.model(fig_id).order())
    R.check(got == expected,
            "figure %r carries %d trace(s) (got %d)" % (fig_id, expected, got))

R.check(board.model("mir_pitch").x_label != board.model("mir_piezo").x_label,
        "4C and 5C are on figures with different x units (%r vs %r)"
        % (board.model("mir_pitch").x_label, board.model("mir_piezo").x_label))

empty = [f[0] for f in app._FIGURE_DEFS if board.model(f[0]).is_empty()]
R.check(not empty,
        "every figure received its scan%s" % (" (empty: %s)" % empty if empty else ""))

# Every one of the nine routed scans must be represented by at least one
# trace (stripping any "#<pass>" suffix down to its substep key), and no two
# traces anywhere on the board -- across passes and across figures -- may
# share a legend label.
base_keys = sorted({sr.key.split("#", 1)[0] for f in app._FIGURE_DEFS
                    for sr in board.model(f[0]).order()})
R.check(base_keys == sorted(app._SCAN_ROUTES),
        "all nine scans are represented on their figures: %s" % base_keys)

labels = [sr.label for f in app._FIGURE_DEFS for sr in board.model(f[0]).order()]
R.check(len(labels) == len(set(labels)),
        "every trace has a distinct legend entry: %s" % labels)

# Distinct colours per figure -- unless a figure now legitimately carries
# more passes than the fixed-size colour palette, in which case colours must
# repeat and that repetition is not a bug.
colours_ok = all(
    len({sr.color for sr in board.model(f[0]).order()}) == len(board.model(f[0]).order())
    or len(board.model(f[0]).order()) > len(app.SERIES_COLORS)
    for f in app._FIGURE_DEFS)
R.check(colours_ok, "traces sharing a figure have distinct colours "
        "(unless there are more passes than palette colours)")

# The property that actually matters, and the one the old interleaved-passes
# bug violated: a scan pass measures each x once, so within any one trace x
# must be strictly increasing, with no x value repeated. Duplicated x is
# exactly what a fine pass re-measuring a coarse pass's region produced when
# both passes shared one trace.
unsorted_series = [sr.label for f in app._FIGURE_DEFS
                   for sr in board.model(f[0]).order()
                   if any(b <= a for a, b in zip(sr.xs, sr.xs[1:]))]
R.check(not unsorted_series,
        "every trace's x is strictly increasing with no duplicates%s"
        % (" (bad: %s)" % unsorted_series if unsorted_series else ""))

R.check(all(len(sr.raw) == len(sr.xs) for f in app._FIGURE_DEFS
            for sr in board.model(f[0]).order()),
        "acquisition order is preserved alongside the sorted draw order")

snap = board.snapshot()
R.check(isinstance(snap, dict) and len(snap.get("figures", [])) == 8
        and snap.get("meta", {}).get("row"),
        "snapshot() returns the run as plain data for a future history tab")

# ── Focus. The left pane must show the running scan every time. This used to
#    skip the left pane whenever the right pane already showed that figure, so
#    3C and 4C moved nothing at all and the scan looked like it vanished.
focus = []
right_seen = set()


def _watch(key, status):
    if status != "running" or key not in app._SCAN_ROUTES:
        return
    fig = app._SCAN_ROUTES[key][0]
    dev = board.model(fig).device
    shown_dev = board._device_tabs.tabText(board._device_tabs.currentIndex()).strip()
    panes = board._devices[dev]
    focus.append((key, fig, dev, shown_dev, panes.left.current_fig()))
    right_seen.add((dev, panes.right.current_fig()))


win.setup_tab.sim_check.setChecked(True)
win.energy_tab.table.selectRow(0)
win.alignment_tab.set_chapter_enabled(4, True)
_done = {}
win.alignment_tab.alignment_done.connect(lambda ok: _done.setdefault("ok", ok))
right_before = {d: v.right.current_fig() for d, v in board._devices.items()}
win._start_alignment()
win.alignment_tab._worker.substep_status.connect(_watch)
H.pump(lambda: "ok" in _done, 180000)

R.check(_done.get("ok") is True, "the focus-tracking run completes")
R.check(len(focus) == 9, "all nine scans were observed starting (%d)" % len(focus))
wrong_tab = [(k, d, sd) for k, f, d, sd, lp in focus if d != sd]
R.check(not wrong_tab, "the device tab follows every scan%s"
        % (" (wrong: %s)" % wrong_tab if wrong_tab else ""))
wrong_pane = [(k, f, lp) for k, f, d, sd, lp in focus if lp != f]
R.check(not wrong_pane, "the left pane shows every running scan%s"
        % (" (wrong: %s)" % wrong_pane if wrong_pane else ""))

right_after = {d: v.right.current_fig() for d, v in board._devices.items()}
R.check(right_after == right_before,
        "the browse pane is never moved by a run (%s -> %s)"
        % (right_before, right_after))

# ── Per-step enable. Every substep is now gated, and a disabled producer means
#    its consumer works from the live position rather than a remembered result.
def run_with(enabled):
    """Run with an explicit enable set. Returns (ok, statuses seen)."""
    seen = []
    result = {}
    win.setup_tab.sim_check.setChecked(True)
    win.energy_tab.table.selectRow(0)
    win.alignment_tab.alignment_done.connect(lambda ok: result.setdefault("ok", ok))
    win._start_alignment(enabled=enabled)
    win.alignment_tab._worker.substep_status.connect(
        lambda k, st: seen.append((k, st)))
    win.alignment_tab._worker.confirm_needed.connect(
        lambda _k: H.QTimer.singleShot(10, win.alignment_tab._proceed_clicked))
    H.pump(lambda: "ok" in result, 180000)
    return result.get("ok"), seen


all_keys = set(win.alignment_tab._substep_chk)

# 4A produces slit_peak, which 4C and the post-4E reopen consume. It used to be
# initialised inside 4A, so switching 4A off alone raised NameError.
ok, seen = run_with(all_keys - {"4_4A"})
R.check(ok is True, "a run with 4A disabled completes")
R.check(("4_4A", "skipped") in seen, "4A reports itself skipped")
R.check(("4_4C", "done") in seen, "4C still runs with 4A disabled")

# A chapter-only run: chapter 3 plus chapter 1, which only logs the row.
ch3 = set(win.alignment_tab._chapter_steps[3]) | set(win.alignment_tab._chapter_steps[1])
ok, seen = run_with(ch3)
R.check(ok is True, "a chapter-3-only run completes")
ran = {k for k, st in seen if st == "done"}
R.check(ran <= ch3, "a chapter-only run touches nothing outside that chapter: %s"
        % sorted(ran - ch3))
R.check({"3_3a", "3_3b", "3_3c", "3_3d"} <= ran, "all of chapter 3 ran: %s" % sorted(ran))

# The chapter-run button must produce the same enable set.
win.alignment_tab._run_chapter(3)
H.pump(lambda: win.alignment_tab._running, 5000)
R.check(win.alignment_tab._running, "the chapter run button starts a run")
done_btn = {}
win.alignment_tab.alignment_done.connect(lambda ok: done_btn.setdefault("ok", ok))
H.pump(lambda: "ok" in done_btn, 180000)
R.check(done_btn.get("ok") is True, "the chapter run button's run completes")

win.alignment_tab.set_all_enabled(True)
R.check(win.alignment_tab.enabled_keys() == all_keys, "set_all_enabled restores everything")

# ── 4F. Reopening the mirror slits used to happen as an unnamed action outside
#    the 4E guard: no step row, no tick box, and it ran even with 4E off.
R.check("4_4F" in win.alignment_tab._substep_chk, "4F has a tick box of its own")
R.check("4_4F" in app.AlignmentTab._SUBSTEP_TEXT, "4F has a step-list label")

slit_target = win.mirror_tab.get_mirror_scan_params()["mir_slit_size_c"]
R.check(abs(app.DEFAULT_MIRROR_SCAN["mir_slit_size_c"] - 1.0) < 1e-9,
        "the shipped default vertical extent is 1 mm (got %s)"
        % app.DEFAULT_MIRROR_SCAN["mir_slit_size_c"])

WR = []
_rp = app.EpicsInterface.put


def _rec(self, pv, value, wait=True, timeout=30.0):
    WR.append((pv, value))
    return _rp(self, pv, value, wait=wait, timeout=timeout)


app.EpicsInterface.put = _rec
top = win.get_pvs()["mir_slit_top"]
bot = win.get_pvs()["mir_slit_bot"]

_all = set(win.alignment_tab._substep_chk)
ok4f, seen4f = run_with(_all)
R.check(ok4f is True, "a run with 4F enabled completes")
R.check(("4_4F", "done") in seen4f, "4F reports done")
gap = [abs(v - bv) for (p1, v), (p2, bv) in zip(WR, WR[1:])
       if p1 == top and p2 == bot]
R.check(gap and abs(gap[-1] - slit_target) < 1e-6,
        "the last slit move opens to %s mm (got %s)"
        % (slit_target, gap[-1] if gap else None))

WR[:] = []
ok_no, seen_no = run_with(_all - {"4_4F"})
R.check(ok_no is True, "a run with 4F disabled completes")
R.check(("4_4F", "skipped") in seen_no, "4F reports itself skipped")
gap_no = [abs(v - bv) for (p1, v), (p2, bv) in zip(WR, WR[1:])
          if p1 == top and p2 == bot]
R.check(not gap_no or abs(gap_no[-1] - slit_target) > 1e-6,
        "with 4F off the slits are not opened (last gap %s)"
        % (gap_no[-1] if gap_no else None))

app.EpicsInterface.put = _rp

# _apply_theme used to reach for the theme label with findChild(QLabel, ""),
# which could return None.
try:
    for name in list(app.THEMES):
        win._theme_combo.setCurrentText(name)
    R.check(True, "every theme applies without raising")
except Exception as exc:
    R.fail("theme switching raised %r" % (exc,))

# SetupTab._stop_monitoring referenced an attribute that does not exist, so
# closing the window raised on every run, simulation included.
try:
    win.close()
    R.check(True, "closing the window does not raise")
except Exception as exc:
    R.fail("closeEvent raised %r" % (exc,))

R.finish()
