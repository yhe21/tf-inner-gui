"""Behavioral checks for the timestamped frame ring (no camera required)."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from frame_capture import FrameRecord, FrameRingBuffer, FrameUnavailable


def record(sequence, timestamp_ns=None):
    timestamp_ns = sequence * 10 if timestamp_ns is None else timestamp_ns
    return FrameRecord(
        sequence=sequence,
        sensor_timestamp_ns=timestamp_ns,
        image=np.full((2, 3, 3), sequence % 256, dtype=np.uint8),
        metadata={"SensorTimestamp": timestamp_ns, "ExposureTime": 5000},
    )


class FrameRingBufferTests(unittest.TestCase):
    def test_default_ring_retains_exactly_latest_200_after_repeated_wraps(self):
        ring = FrameRingBuffer()
        for sequence in range(1, 602):
            ring.append(record(sequence))

        frames = ring.snapshot()
        self.assertIsInstance(frames, tuple)
        self.assertEqual(len(frames), 200)
        self.assertEqual([frame.sequence for frame in frames], list(range(402, 602)))
        self.assertEqual(ring.get_before(6020).sequence, 601)
        self.assertEqual(ring.get_before(4030).sequence, 402)
        with self.assertRaises(FrameUnavailable):
            ring.get_before(4020)

    def test_matching_is_strictly_earlier_including_equal_timestamp(self):
        ring = FrameRingBuffer(capacity=4)
        for sequence, timestamp in enumerate((100, 200, 300), start=1):
            ring.append(record(sequence, timestamp))

        for target, expected in ((101, 100), (199, 100), (200, 100),
                                 (201, 200), (300, 200), (301, 300),
                                 (1000, 300)):
            with self.subTest(target=target):
                frame = ring.get_before(target)
                self.assertEqual(frame.sensor_timestamp_ns, expected)
                self.assertLess(frame.sensor_timestamp_ns, target)

    def test_empty_and_no_earlier_frame_are_explicitly_unavailable(self):
        ring = FrameRingBuffer()
        with self.assertRaises(FrameUnavailable) as empty:
            ring.get_before(100)
        self.assertTrue(empty.exception.reason)

        ring.append(record(1, 100))
        for target in (50, 100):
            with self.subTest(target=target):
                with self.assertRaises(FrameUnavailable) as unavailable:
                    ring.get_before(target)
                self.assertTrue(unavailable.exception.reason)

    def test_max_age_has_inclusive_boundary_and_never_returns_stale_frame(self):
        ring = FrameRingBuffer()
        ring.append(record(1, 90))
        self.assertEqual(ring.get_before(100, max_age_ns=10).sequence, 1)
        with self.assertRaises(FrameUnavailable):
            ring.get_before(100, max_age_ns=9)
        with self.assertRaises(FrameUnavailable):
            ring.get_before(100, max_age_ns=0)

    def test_returned_frame_remains_usable_after_ring_evicts_it(self):
        ring = FrameRingBuffer(capacity=2)
        ring.append(record(1, 100))
        held = ring.get_before(101)
        for sequence in range(2, 10):
            ring.append(record(sequence, sequence * 100))
        self.assertEqual(held.sequence, 1)
        self.assertEqual(held.sensor_timestamp_ns, 100)
        np.testing.assert_array_equal(held.image, np.ones((2, 3, 3), dtype=np.uint8))
        self.assertEqual(held.metadata["SensorTimestamp"], 100)
        self.assertNotIn(1, [item.sequence for item in ring.snapshot()])

    def test_parallel_readers_observe_coherent_ordered_snapshots(self):
        ring = FrameRingBuffer(capacity=200)
        ring.append(record(1))
        start = threading.Barrier(5)
        finished = threading.Event()

        def writer():
            start.wait(timeout=5)
            try:
                for sequence in range(2, 1502):
                    ring.append(record(sequence))
            finally:
                finished.set()

        def reader():
            start.wait(timeout=5)
            checks = 0
            while not finished.is_set() or checks < 30:
                snapshot = ring.snapshot()
                timestamps = [frame.sensor_timestamp_ns for frame in snapshot]
                self.assertLessEqual(len(snapshot), 200)
                self.assertTrue(snapshot)
                self.assertEqual(timestamps, sorted(set(timestamps)))
                for frame in snapshot:
                    self.assertEqual(frame.metadata["SensorTimestamp"], frame.sensor_timestamp_ns)
                    self.assertEqual(int(frame.image[0, 0, 0]), frame.sequence % 256)
                selected = ring.get_before(10**9)
                self.assertLess(selected.sensor_timestamp_ns, 10**9)
                checks += 1
            return checks

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(writer)] + [pool.submit(reader) for _ in range(4)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(ring.snapshot()), 200)
        self.assertEqual(ring.snapshot()[-1].sequence, 1501)


if __name__ == "__main__":
    unittest.main()

class FrameRecordOwnershipTests(unittest.TestCase):
    def test_image_and_metadata_are_owned_immutable_snapshots(self):
        source = np.full((2, 3, 3), 8, dtype=np.uint8)
        metadata = {"SensorTimestamp": 123, "ColourGains": [1.0, 2.0]}
        frame = FrameRecord(1, 123, source, metadata)
        source.fill(99)
        metadata["SensorTimestamp"] = 456
        metadata["ColourGains"][0] = 9.0
        self.assertEqual(int(frame.image[0, 0, 0]), 8)
        self.assertEqual(frame.metadata["SensorTimestamp"], 123)
        self.assertEqual(tuple(frame.metadata["ColourGains"]), (1.0, 2.0))
        with self.assertRaises(ValueError):
            frame.image[0, 0, 0] = 9
        with self.assertRaises(ValueError):
            frame.image.setflags(write=True)
        with self.assertRaises(TypeError):
            frame.metadata["SensorTimestamp"] = 999

    def test_record_rejects_mismatched_metadata_timestamp(self):
        with self.assertRaises(ValueError):
            FrameRecord(1, 123, np.zeros((2, 3, 3), dtype=np.uint8),
                        {"SensorTimestamp": 456})


class FrameRingBufferValidationTests(unittest.TestCase):
    def test_duplicate_or_out_of_order_frame_cannot_corrupt_lookup(self):
        ring = FrameRingBuffer(capacity=3)
        ring.append(record(1, 100))
        ring.append(record(2, 200))
        for timestamp in (200, 199):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    ring.append(record(3, timestamp))
        self.assertEqual([frame.sensor_timestamp_ns for frame in ring.snapshot()], [100, 200])
        self.assertEqual(ring.get_before(201).sensor_timestamp_ns, 200)

    def test_invalid_time_values_are_rejected_without_silent_conversion(self):
        ring = FrameRingBuffer()
        ring.append(record(1, 100))
        for timestamp in (-1, 100.5, True, "101"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    ring.get_before(timestamp)
        for max_age in (-1, 0.5, True):
            with self.subTest(max_age=max_age):
                with self.assertRaises(ValueError):
                    ring.get_before(101, max_age_ns=max_age)

if __name__ == "__main__":
    unittest.main()
