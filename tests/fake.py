"""Test doubles: a fake serial port, and (in later tests) a firmware emulator behind it."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


def wait_until(condition: Callable[[], object], timeout: float = 2.0) -> bool:
    """Polls ``condition`` until it is true; returns False on timeout."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)
    return True


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


class FakeFirmware:
    """Emulates the command handling of the card firmware (``slcan.c``), behind a :class:`FakeSerial`.

    It keeps the mode, the settings, the tables and the response queue, and replies like the
    firmware does, including its BEL cases. There is no LIN bus: tests make frames appear with
    :meth:`report`. ``busy`` makes that many of the next ``t0`` commands fail, as if the previous
    single frame were still being sent.
    """

    def __init__(self, firmware: int = 6, hardware: int = 3, debug: bool = False, chunk: int | None = None):
        self.version = (hardware, firmware)
        self.debug = debug
        self.uid = "0123456789ABCDEF01234567"
        self.port = FakeSerial(self.handle, chunk=chunk)
        self.silent = False
        self.busy = 0
        self.mode = "closed"
        self.baud = 19200
        self.checksum = "enhanced"
        self.diag_classic = True
        self.flags = 0
        self.schedule: list[tuple[int, int]] = []  # (id, slot ms)
        self.responder: list[dict] = []  # {"id", "publish", "length", "data"}
        self.queue: list[bytes] = []
        self.queue_id: int | None = None
        self.reported: set[int] = set()
        self.single_frames: list[tuple[int, bytes]] = []
        self.booted = False

    # Test side.

    def report(self, lin_id: int, data: bytes) -> None:
        """Reports a frame, as if it were seen on the bus."""
        self.port.feed(f"t0{lin_id:02X}{len(data)}{data.hex().upper()}\r".encode())

    def published(self, lin_id: int) -> None:
        """A header of ``lin_id`` came, and the card published its response: reports the publish
        and moves the next queued response in, like the firmware does for a changed response."""
        entry = self._entry(lin_id)
        if entry is None or not entry["publish"]:
            raise AssertionError(f"0x{lin_id:02X} isn't published")
        if lin_id in self.reported:
            self.report(lin_id, entry["data"])
            if self.queue and self.queue_id == lin_id:
                entry["data"] = self.queue.pop(0)

    # Firmware side.

    def _entry(self, lin_id: int) -> dict | None:
        return next((e for e in self.responder if e["id"] == lin_id), None)

    def handle(self, raw: str) -> bytes:
        line = "".join(c for c in raw if c.isascii() and c.isalnum())
        if not line or self.silent:
            return b""
        if len(raw) + 1 > 32:
            return b"\a"
        try:
            reply = self._command(line)
        except (ValueError, IndexError):
            return b"\a"
        if reply is None:
            return b""
        return b"\a" if reply is False else reply.encode() + b"\r"

    @staticmethod
    def _id(text: str) -> int:
        value = int(text, 16)
        if len(text) != 2 or value > 0x3F:
            raise ValueError
        return value

    @staticmethod
    def _len(text: str) -> int:
        value = int(text, 16)
        if not 1 <= value <= 8:
            raise ValueError
        return value

    @staticmethod
    def _slot(text: str) -> int:
        value = int(text, 16)
        if len(text) != 5 or value == 0:
            raise ValueError
        return value

    @staticmethod
    def _payload(text: str, length: int) -> bytes:
        if len(text) != 2 * length:
            raise ValueError
        return bytes.fromhex(text)

    def _tables_have_room(self, schedule: bool) -> bool:
        return len(self.responder) < 32 and (not schedule or len(self.schedule) < 32)

    def _command(self, line: str):
        """Returns the reply text, False for BEL, or None for no reply."""
        cmd, sub = line[0], line[1:2]
        if cmd in "OL":
            if self.mode != "closed":
                return False
            self.mode = "open" if cmd == "O" else "monitor"
            return ""
        if cmd == "C":
            if self.mode == "closed":
                return False
            self.mode = "closed"
            return ""
        if cmd == "V":
            return f"V{self.version[0]:02X}{self.version[1]:02X}"
        if cmd == "N":
            return "N" + self.uid
        if cmd == "F":
            flags, self.flags = self.flags, 0
            return f"F{flags:02X}"
        if cmd == "S":
            if self.mode != "closed" or len(line) != 2:
                return False
            self.baud = {"1": 2400, "2": 9600, "3": 10400}.get(sub, 19200)
            return ""
        if cmd == "x":
            if line not in ("x1", "x2", "x3", "x4"):
                return False
            if sub in "12":
                self.checksum = "enhanced" if sub == "1" else "classic"
            else:
                self.diag_classic = sub == "3"
            return ""
        if cmd == "w":
            self.schedule.clear()
            self.responder.clear()
            self.queue.clear()
            self.queue_id = None
            self.reported.clear()
            return ""
        if line.startswith("R0"):
            if len(line) != 10 or not self._tables_have_room(schedule=True):
                return False
            lin_id, slot, length = self._id(line[2:4]), self._slot(line[4:9]), self._len(line[9])
            self.schedule.append((lin_id, slot))
            self.responder.append({"id": lin_id, "publish": False, "length": length, "data": b""})
            return ""
        if line.startswith(("r0", "r1")):
            if len(line) != 5 or not self._tables_have_room(schedule=sub == "0"):
                return False
            lin_id, length = self._id(line[2:4]), self._len(line[4])
            if sub == "0":
                self.schedule.append((lin_id, ((88 + 20 * length) * 1000 + self.baud - 1) // self.baud))
            self.responder.append({"id": lin_id, "publish": False, "length": length, "data": b""})
            return ""
        if line.startswith("T0"):
            lin_id, slot, length = self._id(line[2:4]), self._slot(line[4:9]), self._len(line[9])
            data = self._payload(line[10:], length)
            if not self._tables_have_room(schedule=True):
                return False
            self.schedule.append((lin_id, slot))
            self.responder.append({"id": lin_id, "publish": True, "length": length, "data": data})
            return ""
        if line.startswith(("t0", "t1", "t2", "t3")):
            lin_id, length = self._id(line[2:4]), self._len(line[4])
            data = self._payload(line[5:], length)
            if sub == "0":
                if self.mode == "closed" or self.busy:
                    self.busy = max(0, self.busy - 1)
                    return False
                self.single_frames.append((lin_id, data))
                return ""
            if sub == "1":
                if not self._tables_have_room(schedule=False):
                    return False
                self.responder.append({"id": lin_id, "publish": True, "length": length, "data": data})
                return ""
            entry = self._entry(lin_id)
            if self.version[1] < 6 or entry is None or not entry["publish"] or entry["length"] != length:
                return False
            if sub == "2":
                entry["data"] = data
                if self.queue_id == lin_id:
                    self.queue.clear()
            else:
                if len(self.queue) >= 16 or (self.queue and self.queue_id != lin_id):
                    return False
                self.queue.append(data)
                self.queue_id = lin_id
            self.reported.add(lin_id)
            return ""
        if line == "boot":
            self.booted = True
            return None
        if line == "d1" and self.debug:
            return "d00000226"
        return False
