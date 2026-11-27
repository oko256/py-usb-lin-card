"""Exceptions raised by the library.

All of them derive from :class:`UsbLinCardError`, so a script can catch everything the library
raises with one ``except`` clause. :class:`ReplyTimeout` is also a :class:`TimeoutError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from usblincard.protocol import Reply


class UsbLinCardError(Exception):
    """Base class of the exceptions of this library."""


class CommandError(UsbLinCardError):
    """The card rejected a command: it replied BEL instead of CR.

    The firmware rejects a command that is malformed, not allowed in the current mode (for example
    changing the baud rate while the channel is open), or that doesn't fit in a full table or queue.

    Attributes:
        command: The command line that was sent, without the CR.
        reply: The reply of the card.
    """

    def __init__(self, command: str, reply: Reply, card: str | None = None):
        """Creates the error.

        Args:
            command: The command line that was sent, without the CR.
            reply: The reply of the card.
            card: Name of the card, for the message.
        """
        prefix = f"{card}: " if card else ""
        super().__init__(f"{prefix}command {command!r} was rejected by the card")
        self.command = command
        self.reply = reply


class ReplyTimeout(UsbLinCardError, TimeoutError):
    """The card didn't reply to a command, or a wait for frames timed out."""


class ProtocolError(UsbLinCardError):
    """The card sent something that doesn't follow the protocol (a malformed reply or frame line)."""


class UnsupportedError(UsbLinCardError):
    """The firmware of the card doesn't support the requested feature (it is too old)."""
