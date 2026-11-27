"""The reader thread that splits the card's output into command replies and frames.

Every command gets exactly one reply terminated with CR (ok) or BEL (error). Replies never start
with ``t``, while received LIN frames are reported asynchronously as lines starting with ``t``, so
the two can be told apart by the first character alone.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from usblincard import protocol as p
from usblincard.errors import ProtocolError, ReplyTimeout, UsbLinCardError
from usblincard.transport import SerialLike

# A line this long without a terminator is garbage (the card's lines are at most ~22 bytes).
_MAX_LINE = 256


class LineReader:
    """Reads the card's output in a thread, and queues the replies and the frames separately.

    A malformed line is dropped and raised as a :class:`ProtocolError` from the next call of
    :meth:`check` (once). If reading the port fails, the reader stops and every later call raises
    the error.
    """

    def __init__(self, port: SerialLike, name: str):
        self.port = port
        self.name = name
        self._cond = threading.Condition()
        self._replies: deque[p.Reply] = deque()
        self._frames: deque[p.Frame] = deque()
        self._protocol_error: ProtocolError | None = None
        self._fatal: UsbLinCardError | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"usblincard-reader-{name}", daemon=True)
        self._thread.start()

    # The thread.

    def _run(self) -> None:
        buf = bytearray()
        try:
            while not self._stop.is_set():
                chunk = self.port.read(self.port.in_waiting or 1)
                now = time.monotonic()
                for b in chunk:
                    if b not in (p.CR, p.BEL):
                        buf.append(b)
                        if len(buf) > _MAX_LINE:
                            self._bad_line(f"line longer than {_MAX_LINE} bytes without a terminator")
                            buf.clear()
                        continue
                    self._line(bytes(buf), b == p.CR, now)
                    buf.clear()
        except Exception as e:
            if self._stop.is_set():
                return
            error = UsbLinCardError(f"{self.name}: reading from the card failed: {e}")
            error.__cause__ = e
            with self._cond:
                self._fatal = error
                self._cond.notify_all()

    def _line(self, raw: bytes, ok: bool, now: float) -> None:
        text = raw.decode("ascii", errors="replace")
        if text.startswith("t"):
            try:
                frame = p.parse_frame(text, now)
            except ProtocolError as e:
                self._bad_line(str(e))
                return
            with self._cond:
                self._frames.append(frame)
                self._cond.notify_all()
        else:
            with self._cond:
                self._replies.append(p.Reply(text, ok))
                self._cond.notify_all()

    def _bad_line(self, message: str) -> None:
        with self._cond:
            self._protocol_error = ProtocolError(f"{self.name}: {message}")

    # The API.

    def check(self) -> None:
        """Raises the error of a failed port, or the latest malformed line (once).

        Raises:
            UsbLinCardError: Reading the port has failed.
            ProtocolError: The card sent a malformed line since the last call.
        """
        with self._cond:
            self._check_locked()

    def _check_locked(self) -> None:
        if self._fatal is not None:
            raise self._fatal
        if self._protocol_error is not None:
            error, self._protocol_error = self._protocol_error, None
            raise error

    def _wait(self, ready, timeout: float) -> bool:
        """Waits with the lock held until ready() is true; returns False on timeout."""
        deadline = time.monotonic() + timeout
        while not ready():
            if self._fatal is not None:
                raise self._fatal
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._cond.wait(left)
        return True

    def reply(self, timeout: float) -> p.Reply:
        """Removes and returns the oldest reply, waiting up to ``timeout`` seconds for one.

        Raises:
            ReplyTimeout: No reply within the timeout.
            UsbLinCardError: Reading the port has failed.
        """
        with self._cond:
            if not self._wait(lambda: self._replies, timeout):
                raise ReplyTimeout(f"{self.name}: no reply within {timeout} s")
            return self._replies.popleft()

    def drain_replies(self) -> list[p.Reply]:
        """Removes and returns the replies that haven't been read."""
        with self._cond:
            replies = list(self._replies)
            self._replies.clear()
            return replies

    def pop_frame(self, timeout: float) -> p.Frame | None:
        """Removes and returns the oldest frame, waiting up to ``timeout`` seconds for one."""
        with self._cond:
            if not self._wait(lambda: self._frames, timeout):
                return None
            return self._frames.popleft()

    def frames(self) -> list[p.Frame]:
        """Returns the buffered frames without removing them."""
        with self._cond:
            return list(self._frames)

    def clear_frames(self) -> None:
        """Drops the buffered frames."""
        with self._cond:
            self._frames.clear()

    def wait_frames(self, count: int, timeout: float) -> list[p.Frame]:
        """Waits until at least ``count`` frames are buffered, and returns them all (not removed).

        Raises:
            ReplyTimeout: Fewer frames within the timeout.
            UsbLinCardError: Reading the port has failed.
        """
        with self._cond:
            if not self._wait(lambda: len(self._frames) >= count, timeout):
                got = [f.line for f in self._frames]
                raise ReplyTimeout(f"{self.name}: got {len(got)}/{count} frames within {timeout} s: {got}")
            return list(self._frames)

    def close(self) -> None:
        """Stops the thread and closes the port."""
        self._stop.set()
        self._thread.join(timeout=1)
        self.port.close()
