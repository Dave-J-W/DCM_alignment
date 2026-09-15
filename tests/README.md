# Tests

Three headless suites. All run offscreen with no display, no EPICS connection and
no IOC, and none of them writes to the real `dcm_config.json` — the harness redirects
the app's auto-save to a temporary file.

```bash
py -3.9 tests/test_simulation.py
py -3.9 tests/test_pv_faults.py
py -3.9 tests/test_config_roundtrip.py
py -3.9 tests/test_preflight_speed.py
py -3.9 tests/test_mini.py
```

Each exits non-zero and prints a `FAILURES:` list if anything regresses.
`test_simulation.py` takes a few minutes, `test_mini.py` about two,
`test_pv_faults.py` about one, and the other two a few seconds.

Green reference on a clean checkout, in the order listed above:
**44 / 36 / 13 / 14 / 48 PASS — 155 total.**

Two things to know before reading a failure as a regression:

- **`test_config_roundtrip.py` needs a `dcm_config.json` to exist**, and it calls
  `R.fail()` rather than skipping when there is none — so on a fresh clone it fails with
  *"no dcm_config.json to round-trip against"*. That is a missing input, not a defect.
  Generate one by launching the console once and closing it; the file is gitignored
  because it holds machine-specific PV names.
- **`_harness.py` forces UTF-8 on stdout/stderr.** Several app strings the suites echo
  contain `→`, and on a default Windows console (cp437/cp1252) printing one used to raise
  `UnicodeEncodeError` from inside `Report.check`, killing the run mid-suite and losing
  every result after that point. If you refactor the harness, keep that.

| File | Covers |
|------|--------|
| `test_simulation.py` | The full 5-step sequence in every skip-mirror / confirm-each-step combination; per-step and chapter-only runs, including that disabling 4A leaves 4C working from the live slit centre; the eight scan figures, their overlays, distinct trace colours and monotonic x; that a blank stage PV cannot hang the checked-read retry loop; that all six computed scan results reach the lookup table; that the JJC stays open at 4 and closes only just before feedback, in both branches; theme switching; clean window close. |
| `test_config_roundtrip.py` | Loads a copy of the real `dcm_config.json`, saves it back through the three settings panels and diffs: no key lost, no value altered, every `DEFAULT_PVS`/`DEFAULT_SCAN` key owned by exactly one panel. Guards the tab split. |
| `test_pv_faults.py` | Stubs the EPICS transport so the fault paths run without hardware: pre-flight blocking the run with zero writes, the fault dialog contents, Check PV diagnosing without resuming, dismissing the dialog leaving the run paused, Try Again resuming once the PVs answer, and Abort stopping cleanly with the interrupted step marked Error. |
| `test_preflight_speed.py` | That pre-flight probes concurrently rather than one PV at a time, that `.RTYP` classification issues no `.DMOV` probe when the record answers, that a silent `.RTYP` falls back to `.DMOV` for that PV alone, that `_rtype` is populated, that report rows keep label order, and that an abort during pre-flight still raises promptly. Reports the measured seconds in its own PASS lines. |
| `test_mini.py` | The mini console's safety properties: that it writes only its five permitted PVs, never reads or writes the DCM piezos and still succeeds with those names blank, never writes H feedback, writes V feedback only to 0, moves each axis to the value it reported (neither scan helper does that itself), refuses an out-of-window write instead of clamping it, writes nothing at all when pre-flight fails, refuses a blank ion chamber, records a threshold skip distinctly from an operator skip, and leaves a `mini` config section that survives a full-console load-and-save cycle. |

`_harness.py` holds the shared setup: offscreen Qt, the import path, the
throwaway config, silenced modal dialogs, an event-loop `pump()` helper and the
pass/fail reporter.

Most of these assertions are regression tests for bugs that were live in the
app — see the commit history for what each one caught.
