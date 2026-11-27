"""The :class:`UsbLinCard` class: one USB LIN Card, and everything it can do."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

import serial

from usblincard import protocol as p
from usblincard._reader import LineReader, SerialLike
from usblincard.discovery import find_card
from usblincard.errors import CommandError, ReplyTimeout, UnsupportedError, UsbLinCardError

#: Serial settings don't matter for a USB CDC port; this is just a common value.
_SERIAL_BAUD = 115200
#: Read timeout of the port, which is how often the reader thread checks whether to stop.
_READ_TIMEOUT = 0.02
#: How long a resync waits for straggling replies after its own, in seconds.
_RESYNC_SETTLE = 0.02


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

    @classmethod
    def find(cls, serial_number: str | None = None, *, timeout: float = 2.0) -> UsbLinCard:
        """Finds a connected card by its USB serial number, and opens it.

        The card is named by its serial number.

        Args:
            serial_number: The USB serial number of the card (see
                :func:`~usblincard.find_cards`). None opens the only card that is connected.
            timeout: Default reply timeout of the commands, in seconds.

        Returns:
            The opened card.

        Raises:
            UsbLinCardError: No card matches, several do (when ``serial_number`` is None), or the
                port can't be opened.
        """
        info = find_card(serial_number)
        return cls(info.path, name=info.serial_number or info.port, timeout=timeout)

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

        Any reply ends up in the reply queue; see :meth:`pending_replies`. The next command first
        drops the replies that are still there (see :meth:`command`).

        Args:
            data: The bytes to write, including any CR terminators.

        Raises:
            UsbLinCardError: The card is closed or its port has failed.
        """
        with self._lock:
            self._write(data)
            self._out_of_sync = True

    def command(self, line: str, timeout: float | None = None) -> p.Reply:
        """Sends one command line and returns the reply, whether it is a success or not.

        After a reply timeout or :meth:`send_raw`, replies may still be on their way that belong
        to earlier commands. Then this first resyncs: it sends a ``V`` command, and drops the
        replies up to and shortly after its reply.

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
        """Drops the late replies of earlier commands.

        It reads the replies up to the reply of a ``V`` command (no other reply starts with
        ``V``), and then drops what arrives shortly after it, in case a late reply was a ``V``
        reply too.
        """
        self._reader.drain_replies()
        self._write(p.VERSION.encode() + b"\r")
        while not self._reader.reply(timeout).text.startswith("V"):
            pass
        time.sleep(_RESYNC_SETTLE)
        self._reader.drain_replies()
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

    # Tables.

    def schedule_header(self, lin_id: int, length: int, slot_ms: int | None = None) -> None:
        """Adds a header to the commander schedule, and subscribes to its response.

        The responses that arrive are reported as frames. This is how the card polls a responder.

        Args:
            lin_id: The frame ID.
            length: The length of the response, 1 to 8 bytes.
            slot_ms: The frame slot time in milliseconds (``R0``). None (``r0``) lets the card
                calculate it from the baud rate and the length (see
                :func:`~usblincard.protocol.auto_slot_ms`).

        Raises:
            ValueError: An argument is out of range.
            CommandError: The schedule or the responder table is full.
        """
        self.command_ok(p.r0(lin_id, length) if slot_ms is None else p.R0(lin_id, slot_ms, length))

    def schedule_frame(self, lin_id: int, data: bytes, slot_ms: int) -> None:
        """Adds a command frame to the commander schedule (``T0``): the card sends the header and publishes ``data``.

        Its publishes aren't reported, unless the response is changed with
        :meth:`replace_response` or :meth:`queue_response`.

        Args:
            lin_id: The frame ID.
            data: The response, 1 to 8 bytes.
            slot_ms: The frame slot time in milliseconds.

        Raises:
            ValueError: An argument is out of range.
            CommandError: The schedule or the responder table is full.
        """
        self.command_ok(p.T0(lin_id, slot_ms, data))

    def subscribe(self, lin_id: int, length: int) -> None:
        """Subscribes to the responses of ``lin_id`` (``r1``); they are reported as frames.

        No header is scheduled: another commander sends it.

        Args:
            lin_id: The frame ID.
            length: The length of the response, 1 to 8 bytes.

        Raises:
            ValueError: An argument is out of range.
            CommandError: The responder table is full.
        """
        self.command_ok(p.r1(lin_id, length))

    def publish(self, lin_id: int, data: bytes) -> None:
        """Publishes ``data`` as the response whenever a header of ``lin_id`` comes (``t1``).

        No header is scheduled: another commander sends it (or the card itself, with
        :meth:`schedule_header`).

        Args:
            lin_id: The frame ID.
            data: The response, 1 to 8 bytes.

        Raises:
            ValueError: An argument is out of range.
            CommandError: The responder table is full.
        """
        self.command_ok(p.t1(lin_id, data))

    def wipe(self) -> None:
        """Wipes the schedule and the responder table (``w``), and the response queue."""
        self.command_ok(p.WIPE)

    def replace_response(self, lin_id: int, data: bytes) -> None:
        """Replaces the response of a published ID in place (``t2``), and clears its queued responses.

        The tables aren't reloaded, so the schedule doesn't restart. From now on, the publishes of
        ``lin_id`` are reported as frames.

        Args:
            lin_id: An ID published with :meth:`publish` or :meth:`schedule_frame`.
            data: The new response, as long as the published one.

        Raises:
            ValueError: An argument is out of range.
            UnsupportedError: The firmware is older than 6.
            CommandError: ``lin_id`` isn't published, or ``data`` has a different length.
        """
        self._require_response_queue()
        self.command_ok(p.t2(lin_id, data))

    def queue_response(self, lin_id: int, data: bytes) -> bool:
        """Queues the next response of a published ID (``t3``).

        After each publish of ``lin_id``, the next queued response replaces its response. When the
        queue is empty, the last response keeps being published. From now on, the publishes of
        ``lin_id`` are reported as frames, so a script can keep the queue filled.

        Args:
            lin_id: An ID published with :meth:`publish` or :meth:`schedule_frame`.
            data: The response, as long as the published one.

        Returns:
            True if it was queued. False if the card rejected it, which for a published ID of the
            right length means that the queue is full (it holds
            :data:`~usblincard.protocol.RESPONSE_QUEUE_DEPTH` responses, all for the same ID).

        Raises:
            ValueError: An argument is out of range.
            UnsupportedError: The firmware is older than 6.
        """
        self._require_response_queue()
        return self.command(p.t3(lin_id, data)).ok

    # Frames.

    def send_frame(self, lin_id: int, data: bytes, timeout: float = 1.0) -> None:
        """Sends one whole frame, the header and the response ``data`` (``t0``).

        The channel must be open, either normally or in monitoring mode. In normal mode, the
        schedule stops for the frame and then restarts from its first entry.

        The card rejects a single frame while its previous one is still being sent, so this retries
        until the card accepts it.

        Args:
            lin_id: The frame ID.
            data: The response, 1 to 8 bytes.
            timeout: How long to retry, in seconds.

        Raises:
            ValueError: An argument is out of range.
            CommandError: The card didn't accept the frame within the timeout (e.g. because the
                channel is closed).
        """
        line = p.t0(lin_id, data)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.command(line)
            if reply.ok:
                return
            if time.monotonic() >= deadline:
                raise CommandError(line, reply, self.name)
            time.sleep(0.002)

    def _timeout(self, timeout: float | None) -> float:
        return self.timeout if timeout is None else timeout

    def receive(self, timeout: float | None = None) -> p.Frame | None:
        """Removes and returns the oldest reported frame, waiting for one if there is none yet.

        Args:
            timeout: How long to wait, in seconds; defaults to :attr:`timeout`. 0 doesn't wait.

        Returns:
            The frame, or None if none came within the timeout.

        Raises:
            ProtocolError: The card sent a malformed line.
            UsbLinCardError: The port has failed.
        """
        self._reader.check()
        return self._reader.pop_frame(self._timeout(timeout))

    def iter_frames(self, timeout: float | None = None) -> Iterator[p.Frame]:
        """Yields the reported frames as they come, removing them.

        Args:
            timeout: Stop when no frame has come for this many seconds; None never stops.

        Yields:
            The frames, oldest first.

        Raises:
            ProtocolError: The card sent a malformed line.
            UsbLinCardError: The port has failed.
        """
        while True:
            frame = self.receive(1.0 if timeout is None else timeout)
            if frame is not None:
                yield frame
            elif timeout is not None:
                return

    def frames(self) -> list[p.Frame]:
        """Returns the reported frames that haven't been removed, without removing them."""
        return self._reader.frames()

    def clear_frames(self) -> None:
        """Drops the reported frames that haven't been removed."""
        self._reader.clear_frames()

    def wait_frames(self, count: int, timeout: float | None = None) -> list[p.Frame]:
        """Waits until at least ``count`` frames have been reported, and returns them all.

        The frames aren't removed: the count includes the ones already there (use
        :meth:`clear_frames` first to wait for new ones).

        Args:
            count: The number of frames to wait for.
            timeout: How long to wait, in seconds; defaults to :attr:`timeout`.

        Returns:
            All the frames, oldest first (at least ``count``).

        Raises:
            ReplyTimeout: Fewer frames came within the timeout.
            ProtocolError: The card sent a malformed line.
            UsbLinCardError: The port has failed.
        """
        self._reader.check()
        return self._reader.wait_frames(count, self._timeout(timeout))

    def wait_frame(self, timeout: float | None = None) -> p.Frame:
        """Waits for the first frame (see :meth:`wait_frames`) and returns it, without removing it.

        Raises:
            ReplyTimeout: No frame came within the timeout.
        """
        return self.wait_frames(1, timeout)[0]

    def collect_frames(self, duration: float) -> list[p.Frame]:
        """Drops the frames reported so far, waits for ``duration`` seconds, and returns the ones reported meanwhile.

        The returned frames stay in the buffer too.
        """
        self.clear_frames()
        time.sleep(duration)
        return self.frames()

    # Helpers.

    def reset(self, baud_rate: int = p.DEFAULT_BAUD_RATE, checksum: p.Checksum = p.Checksum.ENHANCED) -> p.ErrorFlags:
        """Puts the card into a known, quiet state.

        It closes the channel, wipes the tables, sets the baud rate and the checksums (diagnostic
        frames classic), clears the error flags, and drops the frames and the replies that
        haven't been read.

        Args:
            baud_rate: The baud rate to set.
            checksum: The checksum model to set.

        Returns:
            The error flags that were latched before the reset.
        """
        self.close_channel()
        self.wipe()
        self.set_baud_rate(baud_rate)
        self.set_checksum(checksum)
        self.set_diagnostic_classic(True)
        flags = self.read_flags()
        self.clear_frames()
        self.clear_replies()
        return flags

    def request(self, lin_id: int, length: int, timeout: float | None = None, slot_ms: int = 20) -> bytes | None:
        """Sends the header of ``lin_id`` and returns the response of a responder.

        The card has no command for a single header. So this wipes the tables, schedules the
        header (repeating every ``slot_ms`` until a response comes), and wipes the tables again.
        The channel must be open (normal mode). The frames reported before the call are dropped,
        and so are the other frames that arrive meanwhile.

        Args:
            lin_id: The frame ID.
            length: The length of the response, 1 to 8 bytes.
            timeout: How long to wait for the response, in seconds; defaults to :attr:`timeout`.
            slot_ms: The slot time of the scheduled header in milliseconds.

        Returns:
            The response, or None if none came within the timeout.

        Raises:
            ValueError: An argument is out of range.
        """
        deadline = time.monotonic() + self._timeout(timeout)
        self.wipe()
        self.clear_frames()
        self.schedule_header(lin_id, length, slot_ms)
        try:
            while True:
                frame = self.receive(max(0.0, deadline - time.monotonic()))
                if frame is None:
                    return None
                if frame.id == lin_id and len(frame.data) == length:
                    return frame.data
        finally:
            self.wipe()
