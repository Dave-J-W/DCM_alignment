# DCM Alignment Console

PyQt6 desktop application for aligning a Double Crystal Monochromator (DCM).

## Install

```bash
pip install -r requirements.txt
```

`pyepics` is only needed for real EPICS hardware; Simulation mode runs without it.

### If you are installing into a venv built on Anaconda

Pin Qt to the 6.6 series:

```bash
pip install "PyQt6==6.6.1" "PyQt6-Qt6==6.6.3"
```

PyQt6-Qt6 6.8 and newer are built against the VS2022 C runtime and ship their own
`vcruntime140*.dll` / `msvcp140*.dll` (14.44). An Anaconda-based interpreter loads its
own 14.29 copies at startup, before Qt gets a say, and Qt then fails with:

```
ImportError: DLL load failed while importing QtCore: The specified procedure could not be found.
```

`os.add_dll_directory` does not help — the conflicting DLL is already resident. Either pin
Qt as above, or use a non-Anaconda Python. Pinning both packages matters: installing
`PyQt6==6.6.1` alone leaves `PyQt6-Qt6` at whatever version was already present.

## Run

```bash
py -3.9 dcm_align_app.py
```

The `py` launcher is not present on every beamline box, and a bare `python` may resolve to
the Microsoft Store stub and fail. If neither works, call the interpreter by absolute path.

There is a second, much smaller GUI for a three-scan touch-up run — see
[Mini Alignment Console](#mini-alignment-console) at the end of this file:

```bash
py -3.9 dcm_mini_app.py
```

## Tabs

| Tab | Purpose |
|-----|---------|
| **Energy Table** | The energy lookup table (MonoE, UE, harmonic, roll, pitch, detector sensitivities) and, below it, the record of past alignments. Select a row before running. |
| **PV Monitor / Config** | Shared PVs — undulator, BPM, feedback, AutoFeedback, ion chamber — with live readbacks, the shared timeouts, Simulation mode, Test EPICS Connection, and Load/Save Config. |
| **DCM Setup** | The DCM crystal motors and piezos, and every DCM scan parameter including the DCM scan signal. |
| **Mirror Setup** | Everything mirror-related in one place: slit and pitch PVs, the mirror scan parameters, the stage in/out table, and the Step 4 procedure summary. |
| **Alignment** | Run the sequence. Per-chapter and per-step tick boxes, a run button on each chapter, the beam-path indicator, tabbed scan figures, BPM readouts and the log. |
| **Record** | Which PVs are written to the lookup table after a successful run. |

All three settings tabs are the same panel with a different filter, so live
readbacks, monitoring and config handling behave identically on each. The saved
`dcm_config.json` is unchanged by the split — `pvs` and `scan` are still single
flat dictionaries, so an existing config loads untouched.

## Alignment Steps

The five top-level steps are called **chapters**. Every chapter clears
`15IDA:userTran6.O` ("AutoFeedback") first — while that field is 1 the transform
forces both feedback loops on (`.G = (a||o)&&...`, `.H = (b||o)&&...`) and any
attempt to control them from here is ignored.

| Step | Action |
|------|--------|
| 1 | Log MonoE, UE, roll and pitch from the selected row |
| 2 | 2A feedback off → 2B undulator, mono, roll, pitch **and the row's detector sensitivities**, waiting for the undulator to arrive → 2C mirror out |
| 3 | 3A centre DCM piezos at 5 → 3B pitch scan (peak) → 3C roll scan (BPM x = 0) → 3D pitch scan (peak) |
| 4 | 4A slit scan → 4B mirror in → **4B2 centre the mirror pitch piezo at 5** → **4C mirror pitch motor scan (BPM y = 0)** → 4D VDM:Y peak → 4E coupled VFM:Y + VDM:Y |
| 5 | Close the JJC → 5A H feedback on → 5B DCM piezo peak → 5C mirror piezo (BPM y = 0) → 5D V feedback on |

4C drives the pitch **motor** (`ID15A1:DMS:VDM:PI`, µrad), not the piezo — 4B2
parks the piezo mid-range first, mirroring what 3A/3C do for the DCM. Both piezo
records report `DRVL=-2, DRVH=12`, so 5 is genuinely the middle.

### Choosing what to run

Every chapter and every step has a tick box in the left panel; unticking a
chapter unticks its steps, and a partly-ticked chapter shows as such. The **▶**
button on a chapter row runs that chapter on its own without disturbing the tick
boxes. Selections reset to all-enabled each launch, so a forgotten unticked box
cannot silently skip a step days later.

A skipped step never leaves a later one stranded: where a step would have
produced a value — 4A's slit centre, 3B's coarse pitch — the sequence reads the
live position instead. The mirror is inserted before Step 5 whenever it is
actually out, tracked from what ran rather than inferred from a checkbox.

### Beam availability

Every scan first checks the upstream shutter, `S15ID-PSS:SCS:BeamBlockingM`.
It is a bi record with `ZNAM="OFF"` / `ONAM="ON"`, and 0 means nothing is
blocking the beam. If it reads blocking, the run pauses with the usual fault
dialog and waits — the PSS owns the shutter, so unlike the feedback loops this
is not something the app may put right on your behalf. Open it and press
**Try Again**.

Note the record sits at MAJOR severity even when the beam *is* available, so
alarm severity is deliberately not used as the test.

### Feedback during the sequence

Both loops are off from 2A onward. Before every scan the app **reads** the
feedback state back and, if a loop is on, turns it off and logs it. From 5A the
H loop is deliberately on, so only V is asserted off; 5D enables V as the final
handover.

### Zero-crossing scans

The three BPM-zero scans — 3C, 4C and 5C — start where you configure them and
step at the configured step size, but have no pre-decided end: they stop as soon
as **two points lie past the zero crossing**. If no crossing appears within three
times the configured span the run pauses with the usual fault dialog. Previously
they swept a fixed window and, if the crossing fell outside it, silently moved
the motor to the closest-to-zero point.

### JJC slit size

The whole alignment runs with the JJC (`15IDC:Slit4VDsize.VAL`) **open at 4**.
It is closed to its operating size — **JJC size before feedback** on the Mirror
tab, default 0.4 mm — as the first substep of Step 5, immediately before the
feedback loops are enabled. This happens whether Step 4 ran or was skipped, so
the mirror is in position either way.

Note that `mirror_in_out.docx` lists 0.4 as the JJC Size *in* value. That is the
post-alignment value; using it as the mirror-in value would close the slit
during Step 4. The mirror-stage default is therefore 4 for both in and out.

## Scan Figures

The figure area has two levels of tabs. The outer level picks the **device** —
DCM or Mirror — and inside each device there are two side-by-side panes, each
with its own tab bar, so you can view any two of that device's figures at once.

| Device | Figure | x axis | Traces |
|--------|--------|--------|--------|
| DCM | Pitch motor | µrad | 3B coarse **and** 3D fine, overlaid |
| DCM | Roll | µrad | 3C |
| DCM | Pitch piezo | DCOM | 5B |
| Mirror | Slit | mm | 4A |
| Mirror | Mirror piezo | DCOM | 4C **and** 5C, overlaid |
| Mirror | VDM:Y | µm | 4D |
| Mirror | VFM:Y | µm | 4E |

The piezo scans have their own figures because their x axis is a DCOM demand of
order 5, not a motor position of order 1300 — putting them on the same axis as
the motor scans made both unreadable.

Each scan added to a figure gets its own colour, its own legend entry and its
own peak/zero marker in the matching colour. Trace colours are fixed and do not
change with the theme, so a colour means the same trace in every screenshot.

The two panes have fixed roles, labelled above each one:

- **LIVE SCAN** (left) always shows whatever is scanning. When a scan starts the
  device tab switches to its device and the pane brings its figure forward,
  every time. There is no opt-out — if you move it, the next scan takes it back.
- **BROWSE** (right) is yours. The app never changes it, including between
  runs, so you can park a finished scan there and keep it in view.

Both panes are independent views of the same data, so the same figure may be
open in both, each with its own zoom.

The live pane resets to the first figure at the start of each run; the browse
pane stays where you left it. Figure data clears at the start of each run.

## PV Fault Handling

The sequence never guesses. Any PV read that comes back `None` — a disconnected
PV, an unknown name, or a channel-access error — is a **critical fault**, not a
silently substituted zero. The same applies to failed writes and to a motor that
never reports `.DMOV`.

**Pre-flight.** Before Start Alignment moves anything (in particular before
step 2A switches the BPM feedback off), every PV the run will touch is
connect-tested. The set is computed from the current configuration, so it
respects *Skip mirror alignment*, blank PV names, and the selected signal
sources. If any PV fails, the run does not start and the log lists them.

**During the run.** On a fault the worker thread stops where it is, logs
`CRITICAL — PV FAULT` naming the PV and the step it was in, and opens a dialog:

| Button | Effect |
|--------|--------|
| **Check PV** | Connect to that PV right now and report value, type, severity, timestamp and IOC host — plus its `.RBV` and `.DMOV` fields. Does not resume. |
| **Try Again** | Re-read the PV. If it answers, the sequence continues from exactly where it stopped. |
| **Abort Alignment** | Stop the run. |

The dialog is deliberately **not** application-blocking: the sequence is halted
because the worker thread is blocked, so you can still watch the live readouts,
the plots and the log, and switch to the Setup tab to diagnose. Closing the
dialog does **not** resume — the run stays paused and a red
`Review fault…` button reopens it.

**Motion is never timed out.** A stage is allowed to take as long as it takes —
CRL Y needs about a minute for its in/out travel and a large Mono E change takes
several minutes. Completion is read from the motor record itself (`.DMOV`,
`.DIFF` against `.RDBD`, `.MISS`, `.LVIO`, the `MSTA` problem bits), and PVs are
classified as motor or plain records at pre-flight by whether their `.DMOV`
connects. Three things are timed, and all of them mean *no confirmation
arrived*, not *this is taking a while*:

| Setup parameter | Default | Fires when |
|---|---|---|
| PV write ack timeout | 10 s | A plain record's put-callback never came back |
| Motor start grace | 5 s | The setpoint was accepted but the motor never started |
| Motor stall timeout | 30 s | `.DMOV` says moving but `.RBV` has stopped advancing |

The mirror stages and the undulator are now genuinely waited for: all setpoints
are issued first so the stages travel concurrently, then each is awaited, and
Step 3 does not begin until the undulator's busy flag clears.

**Live monitors.** While a run is in progress, a PV that drops out between reads
is caught by its channel-access connection callback and pauses the sequence at
the next safe point. When no run is in progress a disconnect is shown as a red
`DISCONNECTED` readback in Setup and a status-bar message, with no dialog.

## Simulation Mode

When **Simulation mode** is checked in Setup (default), all motor moves and scans are
replaced by Gaussian/linear synthetic signals. No EPICS connection is required.
Uncheck to connect to real hardware (requires `pyepics` and a running IOC).

Simulation never raises a PV fault and skips pre-flight.

## Scan Signal

The Step 3 pitch scans and the Step 5B DCM piezo scan read whichever detector is
selected as **DCM scan signal** in Setup (default: BPM Intensity). Step 4's
mirror scans have their own **Signal source** selector on the Mirror tab.

## Configuration

PV names and scan parameters are editable in the Setup tab. Use **Save Config…**
and **Load Config…** to export/import `.json` configuration files.

## Lookup Table

Column headings word-wrap, and columns are resizable by dragging the dividers —
a recorded row can carry 30-odd columns with names like
"MonP Max Intensity w/o Mirror". Widths survive adding a record and are saved to
`dcm_config.json`. The energy table and the mirror stage table are resizable too.

### CSV format

```
mono_e,ue,harmonic,roll,pitch,bpm_sen,ic_sen_unit,ic_sen_num
8.0,8.02,1,-7670,1338,6,2,3
10.0,10.03,1,-7671,1328,6,2,3
```

---

# Mini Alignment Console

`dcm_mini_app.py` — a second, deliberately small GUI over the same codebase, for one
specific job: a quick touch-up **at whatever energy the beamline is already at**.

> **Status: under development.** This section describes the agreed design. The
> environment notes and the pre-flight speed-up are in place; `dcm_mini_app.py` itself
> lands in a follow-up commit. Nothing here has been run against real hardware yet.

## Why it exists

The motivating case is a beamline **with no DCM piezo**. That one fact drives the whole
design:

- the DCM pitch scan drives the **motor**, because there is no piezo to scan;
- the **mirror** pitch piezo is the only remaining fine vertical actuator, so it is what
  zeroes BPM y;
- vertical feedback is **left off**, because the loop it normally closes has no actuator;
- the full console's step 3A — centre the DCM pitch piezo at 5 — is meaningless here.

The mini console never reads, writes or connect-tests `piezo_pitch` / `piezo_roll`, so it
runs fine with those PV names blank or dead. The full console cannot do this job: its
chapter 3 centres the DCM piezo and its pre-flight requires both piezo PVs.

## The sequence

No energy row is selected. Every scan starts from where its motor PV is *now*.

| Step | Action |
|------|--------|
| 1 | Turn **vertical** feedback off (H is left alone, and never written) |
| 2 | DCM pitch **motor** scan → maximum intensity on the **ion chamber** |
| 3 | Read BPM x. Within the threshold (default **12 µm**) → **skip**. Otherwise scan the DCM roll **motor** → BPM x = 0 |
| 4 | Mirror pitch **piezo** scan → BPM y = 0 |

Both scans are documented steps: `steps.docx` lists "Pitch Scan → intensity peak" under
Step 3 and "Mirror piezo pitch scan → BPM y = 0" under Step 5. What is new here is the live
anchoring, the ion chamber as the detector, and the absent DCM piezo.

Step 3 is the only conditional step. The decision is always logged **with the measured
number**, and "skipped because already centred" is recorded distinctly from "skipped
because the operator un-ticked it" — those mean different things.

There is an optional step 5, **off by default**: a re-peak of the pitch motor after a roll
move, mirroring the full console's 3D. 3D exists because a roll move perturbs the pitch
peak; whether that matters here depends on the roll–pitch coupling of this DCM.

### This is not "lock-in"

The full console's 5C exists so that 5D can *engage* the V loop. With V left off, the mini
run pre-positions the mirror piezo and **nothing closes**. The log and every run record say
*"pre-positioned for V lock — V feedback NOT engaged"*, so no record can later be misread
as a closed loop. Re-enabling vertical feedback is the operator's action.

## What it refuses to do

Each of these is a guard, not a warning:

- **A blank `ion_chamber` PV refuses the run.** `_signal_pv` falls back to BPM intensity
  when that name is empty, so a run could otherwise peak on the BPM while the log claimed
  the ion chamber.
- **No write leaves a declared travel window.** Each axis gets a window from its live
  position plus a configured maximum excursion, and an out-of-window write is a *fault,
  never a silent clamp* — a clamping `ao` record plus a scan that records demands rather
  than readbacks would make the plot and the record lie.
- **Drive limits are checked against the real scan budget**, not one scan width. The
  zero-crossing scans walk with no fixed end and extend their budget on every retry. Which
  limit fields to read is taken from the record type: a `motor` record has `.HLM`/`.LLM`,
  an `ao` piezo has `.DRVH`/`.DRVL`.
- **The mirror must be in the beam**, biased toward "in": a stage counts as out only once
  it has travelled past a configurable fraction (default 0.5) of the way to its out
  position. A stage that cannot be read is recorded as "could not verify", which is not the
  same as "out".
- **Nothing moves before pre-flight passes**, so a dead ion chamber stops the run *before*
  vertical feedback is switched off.
- **The V-feedback state is reported on every exit path** — success, PV fault, unexpected
  exception, operator abort. The run most likely to leave the beamline in an odd state is
  an aborted one, so that is exactly the run that must not stay quiet.

The mini run writes **exactly five** PV names and no others: `auto_feedback`,
`feedback_v`, `pitch`, `roll`, `mir_piezo_pitch` — and `roll` only when the BPM x threshold
was exceeded.

## Configuration

The mini console reads the PV names and scan parameters the full console saved, and owns
one new top-level section of `dcm_config.json`:

```json
"mini": {
  "scan":    { "mini_bpm_x_threshold_um": 12.0, "...": "..." },
  "records": [ { "timestamp": "...", "dcm_pitch_urad": 1338.42, "...": "..." } ]
}
```

`mini.scan` holds the mini's own parameters, named `mini_*` so they can never silently
override a value the full console's Setup tab edits. `mini.records` appends one row per
run. Every numeric key carries its unit; `signal_pv` records *which* detector the peak was
actually found on; and the BPM x threshold in force is stored per run, so a row read months
later is interpretable on its own terms.

Both applications preserve top-level keys they do not own and write via a temp file plus
`os.replace`, so neither can delete the other's section or leave truncated JSON.

### Run lock

Both consoles drive the same motors. A lock file beside the config records which app is
running, its pid, host and start time; each refuses to start a run while the other holds a
live lock, naming the holder. A stale lock can be overridden.

## Simulation

The mini console has its own Simulation checkbox, defaulting to the `simulate` value in
`dcm_config.json` but never writing it back. In simulation the guards that read drive
limits are skipped — `EpicsInterface.get` returns `0.0` for any unset PV, so `DRVL` and
`DRVH` would both read zero and a naive guard would refuse every simulated run.
