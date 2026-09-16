# ==============================================================================
# QNX Real-Time Digital Twin - Thread-Safe Multi-Rate Ring Buffers
# Implements POSIX-style lock-protected bounded ring queues for concurrent
# multi-rate sensor data ingestion and atomic snapshot extraction.
# ==============================================================================

import time
import threading
from collections import deque

class StreamSample:
    """Represents a single telemetry data packet timestamped at ingestion."""
    def __init__(self, stream_id, value, timestamp_ms=None, seq_id=0):
        self.stream_id = stream_id
        self.value = value
        self.timestamp_ms = timestamp_ms if timestamp_ms is not None else (time.time() * 1000.0)
        self.seq_id = seq_id

    def age_ms(self, current_time_ms=None):
        """Calculates sample age relative to current time in milliseconds."""
        now = current_time_ms if current_time_ms is not None else (time.time() * 1000.0)
        return max(0.0, now - self.timestamp_ms)

    def __repr__(self):
        return f"<Sample {self.stream_id}: val={self.value} t={self.timestamp_ms:.2f}ms seq={self.seq_id}>"

class RingBuffer:
    """Thread-safe bounded FIFO queue protecting multi-rate sensor streams."""
    def __init__(self, stream_id, capacity=64):
        self.stream_id = stream_id
        self.capacity = capacity
        self._buffer = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq_counter = 0

    def push(self, value, timestamp_ms=None):
        """Thread-safe push of a new sensor sample."""
        with self._lock:
            self._seq_counter += 1
            sample = StreamSample(
                stream_id=self.stream_id,
                value=value,
                timestamp_ms=timestamp_ms,
                seq_id=self._seq_counter
            )
            self._buffer.append(sample)
            return sample

    def peek_latest(self):
        """Thread-safe inspection of the most recent sample without removing it."""
        with self._lock:
            if len(self._buffer) > 0:
                return self._buffer[-1]
            return None

    def pop_latest(self):
        """Thread-safe extraction of the latest sample."""
        with self._lock:
            if len(self._buffer) > 0:
                return self._buffer.pop()
            return None

    def size(self):
        """Returns current buffer item count."""
        with self._lock:
            return len(self._buffer)

    def clear(self):
        """Clears buffer contents."""
        with self._lock:
            self._buffer.clear()

if __name__ == "__main__":
    print("=== Testing Thread-Safe Ring Buffer Implementation ===")
    buf = RingBuffer("traffic", capacity=10)

    def producer():
        for i in range(15):
            sample = buf.push(round(50.0 + i * 2.5, 1))
            print(f"[PRODUCER] Pushed: {sample}")
            time.sleep(0.01)

    t = threading.Thread(target=producer)
    t.start()
    t.join()

    print(f"Buffer size (capacity 10): {buf.size()}")
    latest = buf.peek_latest()
    print(f"Latest Sample Peek: {latest}")
    print(f"Sample Age: {latest.age_ms():.2f} ms")
