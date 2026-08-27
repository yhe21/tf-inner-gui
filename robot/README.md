# VT6 / Raspberry Pi communication setup

`Main.prg` is the TF program with all Raspberry Pi socket access isolated in
`RpiNet`. Configure Epson TCP/IP port `#202` in the controller instead of
hard-coding an address in SPEL+:

- Mode used by the program: Client
- Host: Raspberry Pi wired IPv4 address: `192.168.0.20`
- TCP port: `5000`

The Epson controller is currently `192.168.0.21/24`, so both devices are on
the same wired subnet.

Create only these four Memory I/O labels on unused bit numbers in Epson RC+:

| Label | Writer | Purpose |
| --- | --- | --- |
| `RpiInnerReq` | robot cycle | Request `INNER` capture |
| `RpiGlueReq` | robot cycle | Request `GLUE` capture |
| `RpiNpReq` | robot cycle | Request `NP` capture |
| `RpiCalibReq` | robot cycle | Request the 12 calibration values |

The robot never waits for Raspberry Pi availability. A request bit remains on
while disconnected and is cleared only after `RpiNet` sends the corresponding
command. `INNER`, `GLUE`, and `NP` results are read
asynchronously, so robot motion does not wait for image processing. An NG
result becomes `NO_INNER`, `NO_GLUE`, or `NO_NP`, is sent to the Raspberry Pi for fault
logging, and then stops the robot program.

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

All fatal robot paths call `FatalError` with one text code. It uses one shared
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
