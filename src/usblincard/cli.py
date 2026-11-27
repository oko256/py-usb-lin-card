"""The ``usblincard`` command: quick bench work with a USB LIN Card.

Commands:

``usblincard list``
    Lists the connected cards: USB serial number, port and stable ``/dev/serial/by-id/`` path.

``usblincard info``
    Shows the hardware and firmware versions, the serial number (MCU unique ID) and the latched
    error flags of a card. Reading the flags clears them.

``usblincard monitor [--baud BAUD] [--classic] [--count N] [--duration S]``
    Monitors the bus and prints every frame with its arrival time, until Ctrl-C (or ``--count``
    frames, or ``--duration`` seconds). A header without a response is shown as
    ``(no response)``. The error flags latched meanwhile are printed at the end.

``usblincard send ID DATA... [--baud BAUD] [--classic]``
    Sends one frame, e.g. ``usblincard send 10 DE AD BE EF``, and prints it as seen on the bus.

``usblincard raw COMMAND...``
    Sends firmware commands as they are, e.g. ``usblincard raw V N F``, and prints the replies.

``info``, ``monitor``, ``send`` and ``raw`` use the only connected card, or the one given with
``--serial SERIAL`` (its USB serial number) or ``--port PORT``. ``monitor`` and ``send`` reset the
card first (closing the channel and wiping its tables), and leave it closed. Frame IDs and data
are hexadecimal, with or without ``0x``.

The exit status is 0 on success, and 1 on an error (printed to stderr).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from usblincard import __version__
from usblincard import protocol as p
from usblincard.card import UsbLinCard
from usblincard.discovery import find_cards
from usblincard.errors import UsbLinCardError


def _hex_int(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a hexadecimal number: {text!r}") from None


def _hex_bytes(parts: list[str]) -> bytes:
    text = "".join(parts).replace("0x", "").replace("0X", "").replace(",", "")
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"not hexadecimal bytes: {' '.join(parts)!r}") from None


def _open(args: argparse.Namespace) -> UsbLinCard:
    """Opens the card the arguments select."""
    if args.port is not None:
        return UsbLinCard(args.port, timeout=args.timeout)
    return UsbLinCard.find(args.serial, timeout=args.timeout)


def _flag_names(flags: p.ErrorFlags) -> str:
    members = [f for f in p.ErrorFlags if f]
    names = [str(f.name) for f in members if f in flags]
    unknown = int(flags) & ~sum(int(f) for f in members)
    if unknown:
        names.append(f"0x{unknown:02X}")
    return ", ".join(names) if names else "none"


def _cmd_list(_args: argparse.Namespace) -> int:
    cards = find_cards()
    if not cards:
        print("No USB LIN Cards found")
    for card in cards:
        print(f"{card.serial_number or '?':<14} {card.port:<14} {card.by_id_path or ''}".rstrip())
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    with _open(args) as card:
        version = card.version()
        print(f"Card:          {card.name}")
        print(f"Hardware:      {version.hardware}")
        print(f"Firmware:      {version.firmware}")
        print(f"Serial number: {card.serial_number()}")
        print(f"Error flags:   {_flag_names(card.read_flags())}")
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    with _open(args) as card:
        card.reset(args.baud, p.Checksum.CLASSIC if args.classic else p.Checksum.ENHANCED)
        card.open_monitor()
        print(f"Monitoring at {args.baud} baud, Ctrl-C to stop", file=sys.stderr, flush=True)
        start = time.monotonic()
        count = 0
        try:
            while args.count is None or count < args.count:
                left = None if args.duration is None else args.duration - (time.monotonic() - start)
                if left is not None and left <= 0:
                    break
                frame = card.receive(0.1 if left is None else min(0.1, left))
                if frame is not None:
                    count += 1
                    print(f"{frame.timestamp - start:10.3f}  {frame}", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            card.close_channel()
        flags = card.read_flags()
        if flags:
            print(f"Error flags: {_flag_names(flags)}", file=sys.stderr)
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    data = _hex_bytes(args.data)
    with _open(args) as card:
        card.reset(args.baud, p.Checksum.CLASSIC if args.classic else p.Checksum.ENHANCED)
        # Monitoring mode reports the frame as it was on the bus.
        card.open_monitor()
        try:
            card.send_frame(args.id, data)
            frame = card.receive(1.0)
        finally:
            card.close_channel()
        if frame is None:
            print("Sent, but the frame wasn't seen on the bus (is the bus powered?)", file=sys.stderr)
            return 1
        print(frame)
    return 0


def _cmd_raw(args: argparse.Namespace) -> int:
    with _open(args) as card:
        failed = False
        for line in args.commands:
            reply = card.command(line)
            failed |= not reply.ok
            print(f"{line} -> {reply.text!r} {'ok' if reply.ok else 'BEL'}")
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usblincard",
        description="Quick bench work with a USB LIN Card.",
        epilog="Frame IDs and data are hexadecimal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the connected cards").set_defaults(func=_cmd_list)

    def card_args(sp: argparse.ArgumentParser) -> None:
        which = sp.add_mutually_exclusive_group()
        which.add_argument("-s", "--serial", help="USB serial number of the card (see 'list')")
        which.add_argument("-p", "--port", help="serial port of the card, e.g. /dev/ttyACM0")
        sp.add_argument("--timeout", type=float, default=2.0, help="reply timeout in seconds (default: %(default)s)")

    def bus_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-b", "--baud", type=int, default=19200, choices=[int(r) for r in p.BaudRate], help="LIN baud rate"
        )
        sp.add_argument("--classic", action="store_true", help="use the classic checksum (LIN 1.x)")

    sp = sub.add_parser("info", help="show the versions, serial number and error flags")
    card_args(sp)
    sp.set_defaults(func=_cmd_info)

    sp = sub.add_parser("monitor", help="print the frames on the bus")
    card_args(sp)
    bus_args(sp)
    sp.add_argument("-n", "--count", type=int, help="stop after this many frames")
    sp.add_argument("-d", "--duration", type=float, help="stop after this many seconds")
    sp.set_defaults(func=_cmd_monitor)

    sp = sub.add_parser("send", help="send one frame")
    card_args(sp)
    bus_args(sp)
    sp.add_argument("id", type=_hex_int, help="frame ID, e.g. 10 or 0x10")
    sp.add_argument("data", nargs="+", help="1 to 8 data bytes, e.g. DEADBEEF or DE AD BE EF")
    sp.set_defaults(func=_cmd_send)

    sp = sub.add_parser("raw", help="send firmware commands and print the replies")
    card_args(sp)
    sp.add_argument("commands", nargs="+", metavar="COMMAND", help="a command line without the CR, e.g. V")
    sp.set_defaults(func=_cmd_raw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the ``usblincard`` command.

    Args:
        argv: The arguments without the program name; defaults to ``sys.argv[1:]``.

    Returns:
        The exit status.
    """
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (UsbLinCardError, ValueError) as e:
        print(f"usblincard: {e}", file=sys.stderr)
        return 1
