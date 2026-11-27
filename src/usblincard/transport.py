"""The serial port interface that :class:`~usblincard.card.UsbLinCard` needs.

A :class:`serial.Serial` has all of it. Test doubles and other transports can implement it and be
given to :class:`~usblincard.card.UsbLinCard` as its ``transport``.
"""

from __future__ import annotations

from typing import Protocol


class SerialLike(Protocol):
    """What the reader needs from a serial port (a :class:`serial.Serial` has all of it)."""

    @property
    def in_waiting(self) -> int:
        """Bytes ready to be read."""
        ...

    def read(self, size: int = 1) -> bytes:
        """Reads up to ``size`` bytes, returning what arrived within the port's timeout."""
        ...

    def write(self, data: bytes) -> int | None:
        """Writes ``data``."""
        ...

    def flush(self) -> None:
        """Waits until the written data has been sent."""
        ...

    def close(self) -> None:
        """Closes the port."""
        ...
