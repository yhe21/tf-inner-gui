# VT6 / Raspberry Pi communication setup

This folder is for the **VT6**, not the separate T6 controller. Its
[`Main.prg`](Main.prg) is the TF program with all Raspberry Pi socket access isolated in
`RpiNet`. Configure Epson TCP/IP port `#202` in the controller instead of
hard-coding an address in SPEL+:

- Mode used by the program: Client
- Host: Raspberry Pi wired IPv4 address: `192.168.0.20`
- TCP port: `5000`

The Epson controller is currently `192.168.0.21/24`, so both devices are on
the same wired subnet.

Create these five Memory I/O labels on unused bit numbers in Epson RC+:

| Label | Writer | Purpose |
| --- | --- | --- |
| `RpiInnerReq` | robot cycle | Request `INNER` capture |
| `RpiGlueReq` | robot cycle | Request `GLUE` capture |
| `RpiNpReq` | robot cycle | Request `NP` capture |
| `RpiCalibReq` | robot cycle | Request the 12 calibration values |
| `RpiNgStopReq` | `RpiNet` (set), `Init` (clear) | Latch any camera NG for the Pick_Part stop checkpoint |

The robot never waits for Raspberry Pi availability. A request bit remains on
while disconnected and is cleared only after `RpiNet` sends the corresponding
command. `INNER`, `GLUE`, and `NP` results are read
asynchronously, so robot motion does not wait for image processing. Any NG
immediately sets `RpiNgStopReq`, before attempting to send `NO_INNER`, `NO_GLUE`,
or `NO_NP` to the Raspberry Pi for fault logging. `RpiNet` does not quit tasks
or move the robot. A network send error therefore cannot cancel the latched NG.

`CALIB` is requested near the beginning of `Pick_Part`, but the robot does not
wait for the response. Coordinates start at zero and remain usable while the
RPi is offline. If only part of the response arrives, each available valid
value is applied independently and missing values retain their current value.
The fixed value order is:

```text
NP_X,NP_Y,NP_Z,NP_U,NPS_X,NPS_Y,NPS_Z,NPS_U,DROP_X,DROP_Y,DROP_Z,DROP_U
```

The Raspberry Pi protocol must terminate every command and response with
CR/LF. `Print #202` and `Line Input #202` provide that line-based interface.

Other fatal robot paths still call `FatalError` with one text code. It uses one shared
internal message slot instead of more Memory I/O bits, lets `RpiNet` send the
code when connected, and then executes `Quit All`. Logging is best effort: it
waits at most 0.2 seconds when connected and does not wait at all while offline.

Current fault codes include:

```text
NO_INNER
NO_GLUE
NO_NP
NO_PART_FROM_CONVEYOR
PICK_PART_NO_VAC
PICK_PART_WAIT_TIMEOUT
DROP_PART_WAIT_TIMEOUT
PICK_NP_NO_VAC
NP_LOCATION_INVALID
DROP_NP_WAIT_TIMEOUT
PICK_FIXTURE_WAIT_TIMEOUT
PICK_FIXTURE_NO_VAC
PALLET_NOT_READY
SECOND_PLACE_TIMEOUT
CALI_STOP
```

## Deferred camera NG stop (2026-09-14)

- Add the new **Memory I/O** label `RpiNgStopReq` on an unused bit in RC+;
  no physical I/O or Arduino changes are required for this latch.
- Only one stop checkpoint is added: immediately after the first
  `Move P_Pick_Part +Z(125) +X(33.5) CP` in `Pick_Part`, before its existing
  conveyor-ready check and pickup motion. When the bit is on, execute
  `Wait 0.5`, `Move P_Pick_Part1`, `Wait 1`, then `Quit All`.
- With no NG, that branch is skipped: there is no added timed wait, motion,
  result-ready interlock, or Count reset. Existing CP modifiers, motion points,
  vacuum outputs, tray/eject logic, and other fault handling are unchanged.
- An NG received after this checkpoint is processed at the next `Pick_Part`
  checkpoint. Assembly and normal pallet placement may continue before then,
  as requested; this is deferred inspection handling, not immediate containment.
- Later OK results, duplicate messages, and TCP reconnects do not clear the bit.
  The bit stays on through `Quit All` and is cleared by `Init` on a fresh main
  program start, before `RpiNet` starts. Inspect affected workpieces before
  restarting; do not treat this as an automatic recovery procedure.
- NP remains capture-only: the RPi does not run an NP classifier and replies
  `NP,OK`, including camera-unavailable, queue-full, and capture-failure paths.
  The existing INNER/GLUE commissioning behavior (always reply OK) is also
  unchanged; actual NG messages or a deliberately set Memory I/O bit are
  needed to exercise the new stop branch.
- The 0.5-second dwell is the requested delay, not an independently verified
  standstill measurement. Compile the complete RC+ project and validate this
  retreat path, deceleration timing, and normal cycle time under the site's
  safe test procedure. Offline tests do not model CP interpolation or stop
  other controllers. No robot was moved by these tests.

## Plate-layer debounce (2026-09-08)

The robot source is based on the user's latest `Main.prg` supplied on 2026-09-08,
including its updated pick timing and drop coordinates. Those motion changes
are preserved; the new fix concerns only the layer-change/Count-reset path.

- `Search_pallet` calls `IsLayerChanged(ByRef confirmedLayer)`. The complete
  three-bit layer code must remain the same for **0.5 seconds** before a new
  layer is accepted. Inputs are sampled with a requested **0.01-second** wait.
- An unchanged layer returns immediately. A bounce back to the current layer
  cancels the change without resetting `Count`. Switching to another candidate
  restarts the full stability timer, even if both codes differ from `Layer`.
- `UpdateLayer(confirmedLayer)` echoes that exact snapshot on the existing
  `plateConfirmBit0/1/2` outputs and updates `Layer`; it never rereads inputs.
  Only then does the existing Search_pallet loop reset `Count` to 1.
- The four direct updates in `Init`, `Init_Reset`, and the ejecting branch retain
  their existing timing and use `Call UpdateLayer(ReadPlateLayer)`. The ejecting
  and manual-reset Count resets remain unchanged. No Memory I/O labels, range
  restrictions, error codes, or Arduino handshake steps were added.
- The settings are at the top of `Main.prg`: `LAYER_STABLE_SECONDS`,
  `LAYER_SAMPLE_SECONDS`, and `LAYER_DEBOUNCE_TIMER`. **Timer 5 is reserved for
  this debounce**; this supplied file otherwise uses timers 1-4. Ensure other
  project files/tasks do not reset timer 5 or call the debounce concurrently.
  If other project files call `UpdateLayer` or `IsLayerChanged`, update their
  arguments to match the new signatures as well.
- While candidates continue changing, the function waits and leaves the existing
  `IsPalletOk` off. No new timeout was added; the existing `Drop_Pallet` readiness
  timeout still applies. This is sampled debouncing, not a hardware glitch filter.

SPEL+ argument passing, integer bitwise `And`, and timer syntax were checked
against the [Epson RC+ 7.5 language reference](https://download.epson.biz/robots/ww/data/pdf/en/SPEL%2BRef75.pdf).

Offline regression checks from the repository root (standard Python only):

```text
python -m unittest discover -s robot/tests -v
```

These tests execute a restricted translation of the actual layer functions and
Search_pallet's layer-check prefix with simulated input bits/time. They do not
compile the full program in Epson RC+ or validate controller scheduling or
physical motion. Build the complete project in RC+ and verify unchanged-layer,
brief-bounce, and real-change cases under the site's safe test procedure before
production use. No robot program was started by these tests.
