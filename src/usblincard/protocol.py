"""The serial protocol of the USB LIN Card firmware, without any I/O.

The card speaks an slcan-like text protocol: each command is one line of ASCII terminated with CR,
and gets exactly one reply terminated with CR (success) or BEL (failure). Received LIN frames are
reported asynchronously as lines starting with ``t``.

This module encodes the commands and parses the replies and frame lines. The encoder functions are
named after the firmware commands (``t0``, ``R0``, ...) and return the command line without the
CR. They check their arguments and raise :class:`ValueError` for anything the card would reject,
so a script finds its mistakes before they reach the card. :class:`usblincard.card.UsbLinCard` uses
them, and most scripts don't need this module directly.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from usblincard.errors import ProtocolError

#: The highest LIN frame ID.
LIN_ID_MAX = 0x3F
#: The longest payload of a LIN frame, in bytes.
PAYLOAD_MAX = 8
#: Entries in each of the card's tables (the commander schedule and the responder table).
TABLE_SIZE = 32
#: Responses the response queue (``t3``) holds.
RESPONSE_QUEUE_DEPTH = 16
#: The longest frame slot time of the schedule, in milliseconds (5 hex digits).
SLOT_MS_MAX = 0xFFFFF
#: The longest command line the card accepts, in bytes, including the CR.
LINE_MAX = 32
#: The baud rate the card powers up with.
DEFAULT_BAUD_RATE = 19200
#: The first firmware version with the response queue (``t2`` and ``t3``).
RESPONSE_QUEUE_FIRMWARE = 6

#: Terminator of commands and of successful replies.
CR = 0x0D
#: Terminator of failed replies.
BEL = 0x07

#: Opens the channel, so that the commander and responder run with the configured tables.
OPEN = "O"
#: Opens the channel in monitoring mode, which reports every frame on the bus.
MONITOR = "L"
#: Closes the channel; also cancels a pending single frame.
CLOSE = "C"
#: Wipes both the schedule and the responder table.
WIPE = "w"
#: Reads the hardware and firmware versions (reply ``Vhhff``).
VERSION = "V"
#: Reads the serial number, i.e. the MCU unique ID (reply ``N`` and 24 hex digits).
SERIAL_NUMBER = "N"
#: Reads and clears the latched error flags (reply ``Fxx``).
FLAGS = "F"
#: Reboots into the STM32 USB DFU bootloader; the card doesn't reply.
BOOT = "boot"
#: Reads the stack usage high-water mark (reply ``d`` and 8 hex digits), in Debug builds only.
STACK_USAGE = "d1"


class BaudRate(enum.IntEnum):
    """The LIN baud rates the card supports."""

    #: 2400 baud (``S1``).
    B2400 = 2400
    #: 9600 baud (``S2``).
    B9600 = 9600
    #: 10400 baud (``S3``).
    B10400 = 10400
    #: 19200 baud (``S4``), the default.
    B19200 = 19200

    @property
    def preset(self) -> int:
        """The preset number of the ``Sx`` command (1 to 4)."""
        return list(BaudRate).index(self) + 1


class Checksum(enum.Enum):
    """The checksum model of LIN frames."""

    #: LIN 2.x enhanced checksum, which covers the protected ID too (the default).
    ENHANCED = "x1"
    #: LIN 1.x classic checksum, which covers only the data.
    CLASSIC = "x2"


class ErrorFlags(enum.IntFlag):
    """The error flags the card latches until they are read with ``F``."""

    #: No errors.
    NONE = 0
    #: LIN receiver overrun, so received bytes were lost.
    RX_OVERRUN = 0x01
    #: A received byte had noise and was dropped (typical with collisions or a baud rate mismatch).
    NOISE = 0x04
    #: A received frame overran the previous one before it could be sent to USB.
    DATA_OVERRUN = 0x08
    #: Output to USB didn't fit in the buffer, and was lost.
    USB_TX_OVERRUN = 0x20
    #: Sending a header or a response failed (e.g. timed out).
    LIN_TX_FAILED = 0x40
    #: A malformed frame (sync, parity, checksum), a missing or too short response, an unexpected
    #: BREAK, or a rejected configuration.
    LIN_FRAME_ERROR = 0x80


@dataclass(frozen=True)
class Version:
    """Versions reported by the card (``V`` command).

    Attributes:
        hardware: Hardware version of the card.
        firmware: Firmware version of the card.
    """

    hardware: int
    firmware: int


@dataclass(frozen=True)
class Reply:
    """The reply of the card to a command.

    Attributes:
        text: The reply without its terminator; empty for most commands.
        ok: True if the reply ended with CR (success), False if with BEL (failure).
    """

    text: str
    ok: bool


@dataclass(frozen=True)
class Frame:
    """A LIN frame reported by the card.

    Attributes:
        id: The frame ID (0x00 to 0x3F).
        data: The payload. Empty for a header that got no response (reported in monitoring mode
            only).
        timestamp: When the frame line arrived from the card, in :func:`time.monotonic` seconds.
    """

    id: int
    data: bytes
    timestamp: float = 0.0

    @property
    def header_only(self) -> bool:
        """True for a header that got no response."""
        return not self.data

    @property
    def line(self) -> str:
        """The frame as the card reports it, e.g. ``t0104DEADBEEF``."""
        return f"t0{self.id:02X}{len(self.data)}{self.data.hex().upper()}"

    def __str__(self) -> str:
        """Formats the frame for people, e.g. ``0x10 [4] DE AD BE EF``."""
        payload = " ".join(f"{b:02X}" for b in self.data) if self.data else "(no response)"
        return f"0x{self.id:02X} [{len(self.data)}] {payload}"


# Argument checks.


def _check_id(lin_id: int) -> None:
    if not 0 <= lin_id <= LIN_ID_MAX:
        raise ValueError(f"LIN ID 0x{lin_id:X} out of range (0x00 to 0x{LIN_ID_MAX:02X})")


def _check_length(length: int) -> None:
    if not 1 <= length <= PAYLOAD_MAX:
        raise ValueError(f"payload length {length} out of range (1 to {PAYLOAD_MAX})")


def _check_slot(slot_ms: int) -> None:
    if not 1 <= slot_ms <= SLOT_MS_MAX:
        raise ValueError(f"slot time {slot_ms} ms out of range (1 to {SLOT_MS_MAX})")


def _frame_args(lin_id: int, data: bytes) -> str:
    """Checks and encodes ``iildd...``."""
    _check_id(lin_id)
    _check_length(len(data))
    return f"{lin_id:02X}{len(data)}{bytes(data).hex().upper()}"


# Command encoders.


def t0(lin_id: int, data: bytes) -> str:
    """Encodes ``t0iildd...``: send one whole frame (header and response) once.

    Args:
        lin_id: The frame ID.
        data: The response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the payload length is out of range.
    """
    return "t0" + _frame_args(lin_id, data)


def t1(lin_id: int, data: bytes) -> str:
    """Encodes ``t1iildd...``: publish a response whenever the header of ``lin_id`` is seen.

    Args:
        lin_id: The frame ID.
        data: The response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the payload length is out of range.
    """
    return "t1" + _frame_args(lin_id, data)


def t2(lin_id: int, data: bytes) -> str:
    """Encodes ``t2iildd...``: replace the response of a published ID, and clear its queue.

    Args:
        lin_id: The published frame ID.
        data: The new response, as long as the published one.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the payload length is out of range.
    """
    return "t2" + _frame_args(lin_id, data)


def t3(lin_id: int, data: bytes) -> str:
    """Encodes ``t3iildd...``: queue the next response of a published ID.

    Args:
        lin_id: The published frame ID.
        data: The response to queue, as long as the published one.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the payload length is out of range.
    """
    return "t3" + _frame_args(lin_id, data)


def r0(lin_id: int, length: int) -> str:
    """Encodes ``r0iil``: schedule a header with an automatic slot time, and subscribe to its response.

    Args:
        lin_id: The frame ID.
        length: The length of the response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the length is out of range.
    """
    _check_id(lin_id)
    _check_length(length)
    return f"r0{lin_id:02X}{length}"


def r1(lin_id: int, length: int) -> str:
    """Encodes ``r1iil``: subscribe to the responses of ``lin_id`` (no header is scheduled).

    Args:
        lin_id: The frame ID.
        length: The length of the response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The ID or the length is out of range.
    """
    _check_id(lin_id)
    _check_length(length)
    return f"r1{lin_id:02X}{length}"


def R0(lin_id: int, slot_ms: int, length: int) -> str:
    """Encodes ``R0iitttttl``: schedule a header with a slot time, and subscribe to its response.

    Args:
        lin_id: The frame ID.
        slot_ms: The frame slot time in milliseconds.
        length: The length of the response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: An argument is out of range.
    """
    _check_id(lin_id)
    _check_slot(slot_ms)
    _check_length(length)
    return f"R0{lin_id:02X}{slot_ms:05X}{length}"


def T0(lin_id: int, slot_ms: int, data: bytes) -> str:
    """Encodes ``T0iitttttldd...``: schedule a command frame, whose response the card publishes itself.

    Args:
        lin_id: The frame ID.
        slot_ms: The frame slot time in milliseconds.
        data: The response, 1 to 8 bytes.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: An argument is out of range.
    """
    _check_slot(slot_ms)
    args = _frame_args(lin_id, data)
    return f"T0{args[:2]}{slot_ms:05X}{args[2:]}"


def set_baud_rate(baud_rate: int) -> str:
    """Encodes ``Sx``: set the LIN baud rate (only allowed while the channel is closed).

    Args:
        baud_rate: One of the rates of :class:`~usblincard.protocol.BaudRate`.

    Returns:
        The command line without the CR.

    Raises:
        ValueError: The card doesn't support the baud rate.
    """
    try:
        rate = BaudRate(baud_rate)
    except ValueError:
        supported = ", ".join(str(int(r)) for r in BaudRate)
        raise ValueError(f"the card doesn't support {baud_rate} baud (only {supported})") from None
    return f"S{rate.preset}"


def set_checksum(checksum: Checksum) -> str:
    """Encodes ``x1`` (enhanced checksum) or ``x2`` (classic checksum).

    Args:
        checksum: The checksum model.

    Returns:
        The command line without the CR.
    """
    return Checksum(checksum).value


def set_diagnostic_classic(classic: bool) -> str:
    """Encodes ``x3`` or ``x4``: how diagnostic frames (IDs 0x3C and 0x3D) are checksummed.

    Args:
        classic: True (``x3``, the default) to always use the classic checksum for them, False
            (``x4``) to treat them like the other frames.

    Returns:
        The command line without the CR.
    """
    return "x3" if classic else "x4"


def auto_slot_ms(length: int, baud_rate: int = DEFAULT_BAUD_RATE) -> int:
    """The frame slot time the card calculates for ``r0`` and ``t0``.

    That is the LIN maximum frame time with a 2.0 margin instead of 1.4, i.e. 88 + 20 * length bit
    times, rounded up to whole milliseconds.

    Args:
        length: The payload length in bytes.
        baud_rate: The LIN baud rate.

    Returns:
        The slot time in milliseconds.
    """
    return ((88 + 20 * length) * 1000 + baud_rate - 1) // baud_rate


# Reply and frame line parsers.


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_hex(text: str) -> bool:
    return bool(text) and all(c in _HEX_DIGITS for c in text)


def _hex(text: str, what: str) -> int:
    if not _is_hex(text):
        raise ProtocolError(f"malformed {what} {text!r}")
    return int(text, 16)


def _reply(text: str, prefix: str, digits: int, what: str) -> int:
    if len(text) != len(prefix) + digits or not text.startswith(prefix):
        raise ProtocolError(f"malformed {what} reply {text!r}")
    return _hex(text[len(prefix) :], f"{what} reply")


def parse_frame(line: str, timestamp: float = 0.0) -> Frame:
    """Parses a frame line ``t0iildd...`` (``t0ii0`` for a header without a response).

    Args:
        line: The line without its terminator.
        timestamp: Arrival time to store in the frame.

    Returns:
        The frame.

    Raises:
        ProtocolError: The line is malformed.
    """
    if len(line) < 5 or not line.startswith("t0") or not _is_hex(line[2:]) or len(line) % 2 == 0:
        raise ProtocolError(f"malformed frame line {line!r}")
    lin_id = int(line[2:4], 16)
    data = bytes.fromhex(line[5:])
    if lin_id > LIN_ID_MAX or len(data) != int(line[4], 16):
        raise ProtocolError(f"malformed frame line {line!r}")
    return Frame(lin_id, data, timestamp)


def parse_version(text: str) -> Version:
    """Parses the reply ``Vhhff`` of :data:`VERSION`.

    Raises:
        ProtocolError: The reply is malformed.
    """
    value = _reply(text, "V", 4, "version")
    return Version(hardware=value >> 8, firmware=value & 0xFF)


def parse_serial_number(text: str) -> str:
    """Parses the reply of :data:`SERIAL_NUMBER`.

    Returns:
        The 24 hex digits of the MCU unique ID, in upper case.

    Raises:
        ProtocolError: The reply is malformed.
    """
    _reply(text, "N", 24, "serial number")
    return text[1:].upper()


def parse_flags(text: str) -> ErrorFlags:
    """Parses the reply ``Fxx`` of :data:`FLAGS`.

    Raises:
        ProtocolError: The reply is malformed.
    """
    return ErrorFlags(_reply(text, "F", 2, "flags"))


def parse_stack_usage(text: str) -> int:
    """Parses the reply ``dxxxxxxxx`` of :data:`STACK_USAGE`.

    Returns:
        The stack usage high-water mark in bytes.

    Raises:
        ProtocolError: The reply is malformed.
    """
    return _reply(text, "d", 8, "stack usage")
