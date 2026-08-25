# VT6 error logging

The TCP server recognises exactly three production commands:

```text
INNER
GLUE
CALIB
```

Every other non-empty line is treated as a robot fault code. The server:

1. appends the timestamp, fault code, capture status, and image filename to
   `error_records/error.log`;
2. queues a native-resolution image in the flat `error_records` directory;
3. sends no response for the fault code.

Example files:

```text
error_records/error.log
error_records/20260821_153012_123_PICK_NP_NO_VAC.jpg
```

Fault captures are prioritised ahead of queued INNER/GLUE captures, but an
exposure already in progress is allowed to finish. If the camera is not ready,
the disk is unavailable, or the capture queue is full, that condition is
appended to the same `error.log` file.
