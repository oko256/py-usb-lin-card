"""The :class:`UsbLinCard` class: one USB LIN Card, and everything it can do."""

from __future__ import annotations

import threading
from pathlib import Path
from types import TracebackType

import serial

from usblincard import protocol as p
from usblincard._reader import LineReader, SerialLike
from usblincard.errors import CommandError, ReplyTimeout, UnsupportedError, UsbLinCardError

#: Serial settings don't matter for a USB CDC port; this is just a common value.
_SERIAL_BAUD = 115200
#: Read timeout of the port, which is how often the reader thread checks whether to stop.
_READ_TIMEOUT = 0.02


class UsbLinCard:
    """One USB LIN Card, connected through its serial port.

    Opening the card starts a reader thread that collects the card's output: the replies to
    commands, and the LIN frames the card reports. Use the card as a context manager, or call
    :meth:`close`, so that the port gets closed::

        with UsbLinCard("/dev/ttyACM0") as card:
            print(card.version())

    The methods send one firmware command each (named in their docs) and wait for its reply. A
    rejected command (BEL reply) raises :class:`~usblincard.errors.CommandError`, and a missing
    reply raises :class:`~usblincard.errors.ReplyTimeout`. The methods can be called from
    several threads: each command and its reply are sent and read under a lock.

    The card itself keeps its settings and tables until it is unplugged. Opening it again doesn't
    reset it; :meth:`reset` does.

    Attributes:
        name: Name of the card in messages and errors.
        timeout: Default reply timeout of the commands, in seconds.
    """

    def __init__(
        self,
        port: str | Path | None = None,
        *,
        name: str | None = None,
        timeout: float = 2.0,
        transport: SerialLike | None = None,
    ):
        """Opens the card.

        Args:
            port: The serial port of the card, e.g. ``/dev/ttyACM0``, a ``/dev/serial/by-id/``
                path, or ``COM3``.
            name: Name of the card in messages; defaults to the port.
            timeout: Default reply timeout of the commands, in seconds.
            transport: An already open serial-like object to use instead of opening ``port``
                (e.g. a test double).

        Raises:
            ValueError: Neither or both of ``port`` and ``transport`` were given.
            UsbLinCardError: The port can't be opened.
        """
        if (port is None) == (transport is None):
            raise ValueError("give either a port or a transport")
        if transport is None:
            try:
                transport = serial.Serial(str(port), _SERIAL_BAUD, timeout=_READ_TIMEOUT)
            except (serial.SerialException, OSError) as e:
                raise UsbLinCardError(f"can't open {port}: {e}") from e
        self.name = name or (str(port) if port is not None else "card")
        self.timeout = timeout
        self._lock = threading.RLock()
        self._version: p.Version | None = None
        # A reply timed out: a late reply may still be on its way.
        self._out_of_sync = False
        self._closed = False
        self._reader = LineReader(transport, self.name)

    # Lifetime.

    def close(self) -> None:
        """Stops the reader thread and closes the port. Closing again does nothing.

        This doesn't change the card's state: it keeps running its tables. Call :meth:`reset`
        first to leave it quiet.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reader.close()

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._closed

    def __enter__(self) -> UsbLinCard:
        """Returns the card itself."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        """Closes the card."""
        self.close()

    def __repr__(self) -> str:
        """Names the card."""
        return f"UsbLinCard({self.name!r})"

    # Raw access.

    def _write(self, data: bytes) -> None:
        if self._closed:
            raise UsbLinCardError(f"{self.name}: the card is closed")
        self._reader.check()
        self._reader.port.write(data)
        self._reader.port.flush()

    def send_raw(self, data: bytes) -> None:
        """Writes bytes to the card as they are, without waiting for anything.

        Any reply ends up in the reply queue; see :meth:`pending_replies`.

        Args:
            data: The bytes to write, including any CR terminators.

        Raises:
            UsbLinCardError: The card is closed or its port has failed.
        """
        with self._lock:
            self._write(data)

    def command(self, line: str, timeout: float | None = None) -> p.Reply:
        """Sends one command line and returns the reply, whether it is a success or not.

        Args:
            line: The command without the CR, e.g. ``"R0 10 00032 4"``. Spaces are allowed (the
                card ignores all non-alphanumeric characters).
            timeout: Reply timeout in seconds; defaults to :attr:`timeout`.

        Returns:
            The reply.

        Raises:
            ValueError: The line contains a terminator, or is too long for the card.
            ReplyTimeout: No reply within the timeout.
            ProtocolError: The card sent a malformed line meanwhile.
            UsbLinCardError: The card is closed or its port has failed.
        """
        if "\r" in line or "\a" in line:
            raise ValueError(f"command {line!r} contains a CR or BEL")
        if len(line) + 1 > p.LINE_MAX:
            raise ValueError(f"command {line!r} is longer than the card accepts ({p.LINE_MAX - 1} characters)")
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            if self._out_of_sync:
                self._resync(timeout)
            self._write(line.encode("ascii") + b"\r")
            try:
                return self._reader.reply(timeout)
            except ReplyTimeout:
                self._out_of_sync = True
                raise

    def _resync(self, timeout: float) -> None:
        """Drops the late replies of the commands that timed out.

        It reads the replies up to the reply of a ``V`` command (no other reply starts with ``V``).
        """
        self._reader.drain_replies()
        self._write(p.VERSION.encode() + b"\r")
        while not self._reader.reply(timeout).text.startswith("V"):
            pass
        self._out_of_sync = False

    def command_ok(self, line: str, timeout: float | None = None) -> p.Reply:
        """Sends one command line, and checks that the card accepted it.

        Args:
            line: The command without the CR.
            timeout: Reply timeout in seconds; defaults to :attr:`timeout`.

        Returns:
            The reply.

        Raises:
            CommandError: The card rejected the command.
            ReplyTimeout: No reply within the timeout.
            UsbLinCardError: The card is closed or its port has failed.
        """
        reply = self.command(line, timeout)
        if not reply.ok:
            raise CommandError(line, reply, self.name)
        return reply

    def pending_replies(self) -> list[p.Reply]:
        """Removes and returns the replies nobody has waited for (e.g. after :meth:`send_raw`)."""
        return self._reader.drain_replies()

    def clear_replies(self) -> None:
        """Drops the replies nobody has waited for."""
        self._reader.drain_replies()

    # Channel.

    def open_channel(self) -> None:
        """Opens the channel (``O``): the commander schedule and the responder table start running.

        The schedule starts from its first entry.

        Raises:
            CommandError: The channel isn't closed.
        """
        self.command_ok(p.OPEN)

    def open_monitor(self) -> None:
        """Opens the channel in monitoring mode (``L``): every frame on the bus is reported.

        The tables aren't used in this mode, but single frames can be sent (:meth:`send_frame`).

        Raises:
            CommandError: The channel isn't closed.
        """
        self.command_ok(p.MONITOR)

    def close_channel(self) -> bool:
        """Closes the channel (``C``), and cancels a pending single frame.

        Returns:
            True if the channel was open, False if it was already closed.
        """
        return self.command(p.CLOSE).ok

    # Settings.

    def set_baud_rate(self, baud_rate: int) -> None:
        """Sets the LIN baud rate (``Sx``). The channel must be closed.

        Args:
            baud_rate: 2400, 9600, 10400 or 19200 (see :class:`~usblincard.protocol.BaudRate`).

        Raises:
            ValueError: The card doesn't support the baud rate.
            CommandError: The channel isn't closed.
        """
        self.command_ok(p.set_baud_rate(baud_rate))

    def set_checksum(self, checksum: p.Checksum) -> None:
        """Selects the checksum model (``x1`` or ``x2``); it takes effect immediately.

        Args:
            checksum: Enhanced (LIN 2.x, the default) or classic (LIN 1.x).
        """
        self.command_ok(p.set_checksum(checksum))

    def set_diagnostic_classic(self, classic: bool) -> None:
        """Selects how the diagnostic frames (IDs 0x3C and 0x3D) are checksummed (``x3`` or ``x4``).

        Args:
            classic: True (the default) to always use the classic checksum for them, False to treat
                them like the other frames.
        """
        self.command_ok(p.set_diagnostic_classic(classic))

    # Device.

    def version(self) -> p.Version:
        """Returns the hardware and firmware versions (``V``). The first reply is cached.

        Raises:
            ProtocolError: The reply is malformed.
        """
        if self._version is None:
            self._version = p.parse_version(self.command_ok(p.VERSION).text)
        return self._version

    @property
    def firmware_version(self) -> int:
        """The firmware version of the card (see :meth:`version`)."""
        return self.version().firmware

    @property
    def supports_response_queue(self) -> bool:
        """True if the firmware has the response queue (firmware 6 and later).

        The response queue is used by :meth:`replace_response` and :meth:`queue_response`.
        """
        return self.firmware_version >= p.RESPONSE_QUEUE_FIRMWARE

    def _require_response_queue(self) -> None:
        if not self.supports_response_queue:
            raise UnsupportedError(
                f"{self.name}: the response queue needs firmware {p.RESPONSE_QUEUE_FIRMWARE} or later "
                f"(the card has {self.firmware_version})"
            )

    def serial_number(self) -> str:
        """Returns the serial number of the card (``N``): its MCU unique ID as 24 hex digits.

        This is not the same as the USB serial number (see :func:`usblincard.find_cards`).

        Raises:
            ProtocolError: The reply is malformed.
        """
        return p.parse_serial_number(self.command_ok(p.SERIAL_NUMBER).text)

    def read_flags(self) -> p.ErrorFlags:
        """Reads and clears the latched error flags (``F``).

        Returns:
            The flags latched since the last read; :attr:`~usblincard.protocol.ErrorFlags.NONE`
            if there were no errors.

        Raises:
            ProtocolError: The reply is malformed.
        """
        return p.parse_flags(self.command_ok(p.FLAGS).text)

    def stack_usage(self) -> int:
        """Returns the stack usage high-water mark of the card in bytes (``d1``, Debug builds only).

        Raises:
            CommandError: The firmware is a Release build.
            ProtocolError: The reply is malformed.
        """
        return p.parse_stack_usage(self.command_ok(p.STACK_USAGE).text)

    def reboot_to_dfu(self) -> None:
        """Reboots the card into the STM32 USB DFU bootloader (``boot``) for flashing, and closes it.

        The card doesn't reply, and its serial port disappears.
        """
        with self._lock:
            self._write(p.BOOT.encode() + b"\r")
            self.close()
