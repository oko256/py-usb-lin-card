"""Python library for the USB LIN Card.

The USB LIN Card is a USB to LIN bus adapter that shows up as a serial port and speaks an
slcan-like text protocol. This package talks that protocol, so scripts can use the card as a LIN
commander, a responder or a bus monitor.

The names most scripts need are available from the package itself::

    from usblincard import UsbLinCard
"""

from usblincard.card import UsbLinCard
from usblincard.discovery import CardInfo, find_card, find_cards
from usblincard.errors import CommandError, ProtocolError, ReplyTimeout, UnsupportedError, UsbLinCardError
from usblincard.protocol import BaudRate, Checksum, ErrorFlags, Frame, Reply, Version, auto_slot_ms
from usblincard.transport import SerialLike

__version__ = "1.0.0"

__all__ = [
    "BaudRate",
    "CardInfo",
    "Checksum",
    "CommandError",
    "ErrorFlags",
    "Frame",
    "ProtocolError",
    "Reply",
    "ReplyTimeout",
    "SerialLike",
    "UnsupportedError",
    "UsbLinCard",
    "UsbLinCardError",
    "Version",
    "__version__",
    "auto_slot_ms",
    "find_card",
    "find_cards",
]
