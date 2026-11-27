"""Test doubles: a fake serial port, and (in later tests) a firmware emulator behind it."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class FakeSerial:
    """A serial port in memory.

    What the code under test writes is split into CR-terminated lines and given to ``handler``,
    whose return value (bytes) is what the port then reads back. Tests can also feed output
    directly. ``chunk`` splits the output into reads of at most that many bytes, to exercise the
    reader's line assembly.
    """

    def __init__(self, handler: Callable[[str], bytes] | None = None, chunk: int | None = None, timeout: float = 0.01):
        self.handler = handler
        self.chunk = chunk
        self.timeout = timeout
        self.written = bytearray()
        self.lines: list[str] = []
        self.closed = False
        self.read_error: Exception | None = None
        self._out = bytearray()
        self._in_line = bytearray()
        self._cond = threading.Condition()

    # Test side.

    def feed(self, data: bytes) -> None:
        """Makes ``data`` readable."""
        with self._cond:
            self._out += data
            self._cond.notify_all()

    def fail(self, error: Exception) -> None:
        """Makes the next read raise ``error``."""
        with self._cond:
            self.read_error = error
            self._cond.notify_all()

    # Serial side.

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._out) if self.chunk is None else min(len(self._out), self.chunk)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while not self._out and self.read_error is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._cond.wait(left)
            if self.read_error is not None:
                raise self.read_error
            size = min(size, len(self._out))
            if self.chunk is not None:
                size = min(size, self.chunk)
            data = bytes(self._out[:size])
            del self._out[:size]
            return data

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("port closed")
        self.written += data
        replies = bytearray()
        for b in data:
            if b != 0x0D:
                self._in_line.append(b)
                continue
            line = self._in_line.decode()
            self._in_line.clear()
            self.lines.append(line)
            if self.handler is not None:
                replies += self.handler(line)
        if replies:
            self.feed(bytes(replies))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
