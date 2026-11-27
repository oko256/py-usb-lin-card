"""Finding the USB LIN Cards connected to this computer.

The cards are recognized by their USB vendor and product IDs. Each card has a USB serial number
(e.g. ``1A2B3C4D5E03``), which stays the same whichever port the card gets, so scripts can pick a
card by it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from serial.tools import list_ports

from usblincard.errors import UsbLinCardError

#: USB vendor ID of the card (pid.codes).
USB_VID = 0x1209
#: USB product ID of the card.
USB_PID = 0xBEBE

# The stable symlinks of serial ports on Linux.
_BY_ID_DIR = Path("/dev/serial/by-id")


@dataclass(frozen=True)
class CardInfo:
    """A connected card, as found by :func:`find_cards`.

    Attributes:
        port: The serial port, e.g. ``/dev/ttyACM0`` or ``COM3``.
        serial_number: The USB serial number, e.g. ``1A2B3C4D5E03``, or None if the system
            doesn't tell it.
        by_id_path: The stable ``/dev/serial/by-id/`` symlink of the port (Linux only), or None.
    """

    port: str
    serial_number: str | None
    by_id_path: str | None = None

    @property
    def path(self) -> str:
        """The best path to open the card with: the stable symlink if there is one, else the port."""
        return self.by_id_path or self.port


def _by_id_paths() -> dict[str, str]:
    """Maps the real paths of the ports to their by-id symlinks."""
    try:
        links = sorted(_BY_ID_DIR.iterdir())
    except OSError:
        return {}
    return {os.path.realpath(link): str(link) for link in links}


def find_cards() -> list[CardInfo]:
    """Lists the connected USB LIN Cards, sorted by serial number.

    Returns:
        The cards; empty if there are none.
    """
    by_id = _by_id_paths()
    cards = [
        CardInfo(port.device, port.serial_number, by_id.get(os.path.realpath(port.device)))
        for port in list_ports.comports()
        if port.vid == USB_VID and port.pid == USB_PID
    ]
    return sorted(cards, key=lambda c: (c.serial_number or "", c.port))


def find_card(serial_number: str | None = None) -> CardInfo:
    """Finds one connected card.

    Args:
        serial_number: The USB serial number of the card (case doesn't matter). None finds the
            only card that is connected.

    Returns:
        The card.

    Raises:
        UsbLinCardError: No card matches, or several do (when ``serial_number`` is None).
    """
    cards = find_cards()
    if serial_number is not None:
        cards = [c for c in cards if (c.serial_number or "").upper() == serial_number.upper()]
        if not cards:
            raise UsbLinCardError(f"no USB LIN Card with serial number {serial_number} is connected")
    if not cards:
        raise UsbLinCardError("no USB LIN Card is connected")
    if len(cards) > 1:
        found = ", ".join(f"{c.serial_number} ({c.port})" for c in cards)
        raise UsbLinCardError(f"several USB LIN Cards are connected, choose one by its serial number: {found}")
    return cards[0]
