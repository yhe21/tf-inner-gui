"""Immutable frame records and a bounded, timestamp-ordered RAM ring."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from numbers import Integral
from threading import RLock
from types import MappingProxyType
from typing import Any

import numpy as np


def _nanoseconds(value: int, name: str = "timestamp_ns") -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer in nanoseconds")
    return int(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


class FrameUnavailable(RuntimeError):
    """A lookup could not return a valid frame; reason is a stable machine code."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class FrameRecord:
    """Owns one immutable image and its metadata from the same camera request.

    SensorTimestamp is passed through unchanged. Image bytes are copied at
    construction and cannot be changed by request reuse or a caller. Call
    image.copy() if a downstream operation needs a writable array.
    """

    sequence: int
    sensor_timestamp_ns: int
    image: np.ndarray
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _nanoseconds(self.sequence, "sequence"))
        object.__setattr__(self, "sensor_timestamp_ns",
                           _nanoseconds(self.sensor_timestamp_ns))
        array = np.asarray(self.image)
        if array.dtype.hasobject or array.ndim < 2 or array.size == 0:
            raise ValueError("image must be a non-empty, numeric image array")
        # An immutable bytes backing also prevents setflags(write=True).
        owned = np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)
        object.__setattr__(self, "image", owned)
        metadata = _freeze(dict(self.metadata))
        if "SensorTimestamp" in metadata and metadata["SensorTimestamp"] != self.sensor_timestamp_ns:
            raise ValueError("image record and metadata timestamps disagree")
        object.__setattr__(self, "metadata", metadata)


class FrameRingBuffer:
    """Retains at most capacity frames. Frame timestamps must strictly increase."""

    def __init__(self, capacity: int = 30) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, Integral) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = int(capacity)
        self._frames: deque[FrameRecord] = deque(maxlen=self.capacity)
        self._lock = RLock()
        self._total_appended = 0

    def append(self, record: FrameRecord) -> None:
        if not isinstance(record, FrameRecord):
            raise TypeError("record must be a FrameRecord")
        with self._lock:
            if self._frames and record.sensor_timestamp_ns <= self._frames[-1].sensor_timestamp_ns:
                raise ValueError("frame timestamps must strictly increase")
            self._frames.append(record)
            self._total_appended += 1

    def get_before(self, timestamp_ns: int, max_age_ns: int | None = None) -> FrameRecord:
        """Return the largest retained timestamp STRICTLY LESS than the target."""
        target = _nanoseconds(timestamp_ns)
        max_age = None if max_age_ns is None else _nanoseconds(max_age_ns, "max_age_ns")
        with self._lock:
            if not self._frames:
                raise FrameUnavailable("empty", "No frames have been captured")
            for record in reversed(self._frames):
                if record.sensor_timestamp_ns < target:
                    if max_age is not None and target - record.sensor_timestamp_ns > max_age:
                        raise FrameUnavailable("too_stale", "Nearest earlier frame exceeds max_age_ns")
                    return record
            raise FrameUnavailable("too_old", "No retained frame is strictly earlier than the target")

    def snapshot(self) -> tuple[FrameRecord, ...]:
        """A stable view; keeping it retains these images beyond ring eviction."""
        with self._lock:
            return tuple(self._frames)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "count": len(self._frames),
                "total_appended": self._total_appended,
                "evicted": max(0, self._total_appended - self.capacity),
                "oldest_timestamp_ns": self._frames[0].sensor_timestamp_ns if self._frames else None,
                "latest_timestamp_ns": self._frames[-1].sensor_timestamp_ns if self._frames else None,
                "image_bytes": sum(frame.image.nbytes for frame in self._frames),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
