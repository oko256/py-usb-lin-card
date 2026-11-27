import threading

import pytest
import serial

import usblincard.card
from fake import FakeFirmware, FakeSerial, wait_until
from usblincard import (
    Checksum,
    CommandError,
    ErrorFlags,
    ProtocolError,
    Reply,
    ReplyTimeout,
    UsbLinCard,
    UsbLinCardError,
    Version,
)

# Opening and closing.


def test_needs_a_port_or_a_transport():
    with pytest.raises(ValueError, match="either a port or a transport"):
        UsbLinCard()
    with pytest.raises(ValueError, match="either a port or a transport"):
        UsbLinCard("/dev/null", transport=FakeSerial())


def test_opens_the_port_with_pyserial(monkeypatch):
    opened = {}

    def fake_serial(port, baud, timeout):
        opened.update(port=port, baud=baud, timeout=timeout)
        return FakeSerial()

    monkeypatch.setattr(usblincard.card.serial, "Serial", fake_serial)
    with UsbLinCard("/dev/ttyACM9") as card:
        assert card.name == "/dev/ttyACM9"
        assert repr(card) == "UsbLinCard('/dev/ttyACM9')"
    assert opened == {"port": "/dev/ttyACM9", "baud": 115200, "timeout": 0.02}


def test_open_failure_is_a_library_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise serial.SerialException("could not open port")

    monkeypatch.setattr(usblincard.card.serial, "Serial", fail)
    with pytest.raises(UsbLinCardError, match="can't open /dev/nope: could not open port"):
        UsbLinCard("/dev/nope")


def test_transport_default_name():
    with UsbLinCard(transport=FakeSerial()) as card:
        assert card.name == "card"


def test_context_manager_closes(fw):
    with UsbLinCard(transport=fw.port) as card:
        assert not card.closed
    assert card.closed
    assert fw.port.closed
    card.close()  # again: nothing happens


def test_closed_card_refuses_commands(card):
    card.close()
    with pytest.raises(UsbLinCardError, match="card: the card is closed"):
        card.command("V")
    with pytest.raises(UsbLinCardError, match="closed"):
        card.send_raw(b"V\r")


# Raw commands.


def test_command_returns_the_reply(card, fw):
    assert card.command("V") == Reply("V0306", ok=True)
    assert card.command("Q") == Reply("", ok=False)
    assert fw.port.lines == ["V", "Q"]


def test_command_ok_raises_on_bel(card):
    with pytest.raises(CommandError, match="card: command 'Q' was rejected") as e:
        card.command_ok("Q")
    assert e.value.command == "Q"
    assert not e.value.reply.ok


def test_command_allows_spaces(card, fw):
    card.command_ok("R0 10 00032 4")
    assert fw.schedule == [(0x10, 0x32)]


@pytest.mark.parametrize("line", ["V\r", "V\aX", "x" * 32])
def test_command_rejects_bad_lines(card, line):
    with pytest.raises(ValueError, match="command"):
        card.command(line)


def test_command_timeout(card, fw):
    fw.silent = True
    with pytest.raises(ReplyTimeout, match=r"card: no reply within 0\.05 s"):
        card.command("V", timeout=0.05)


def test_command_uses_the_default_timeout(fw):
    fw.silent = True
    with UsbLinCard(transport=fw.port, timeout=0.05) as card, pytest.raises(ReplyTimeout, match=r"0\.05 s"):
        card.command("V")


def test_late_reply_after_a_timeout_is_dropped(card, fw):
    fw.silent = True
    with pytest.raises(ReplyTimeout):
        card.command("F", timeout=0.05)
    fw.silent = False
    # The reply of the F arrives late, after a stray empty reply.
    fw.port.feed(b"\rF00\r")
    assert card.command("N").text == "N" + fw.uid
    assert fw.port.lines == ["F", "V", "N"]


def test_send_raw_and_pending_replies(card, fw):
    card.send_raw(b"V\rQ\r")
    replies = []
    while len(replies) < 2:
        replies += card.pending_replies()
    assert replies == [Reply("V0306", True), Reply("", False)]
    card.send_raw(b"V\r")
    card.command("V")  # waits for the first reply, which is the raw one's
    card.clear_replies()
    assert card.pending_replies() == []


def test_protocol_error_is_raised_from_the_next_command(card, fw):
    fw.port.feed(b"t0104DE\r")
    assert wait_until(lambda: card._reader._protocol_error is not None)
    with pytest.raises(ProtocolError, match="malformed frame line"):
        card.command("V")
    card.command_ok("V")


def test_commands_from_several_threads(card):
    errors = []

    def worker():
        try:
            for _ in range(50):
                assert card.command_ok("V").text == "V0306"
                assert card.command_ok("F").text == "F00"
        except Exception as e:  # pragma: no cover (only on failure)
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# Channel.


def test_open_and_close_channel(card, fw):
    card.open_channel()
    assert fw.mode == "open"
    with pytest.raises(CommandError):
        card.open_channel()
    assert card.close_channel()
    assert fw.mode == "closed"
    assert not card.close_channel()


def test_open_monitor(card, fw):
    card.open_monitor()
    assert fw.mode == "monitor"
    with pytest.raises(CommandError):
        card.open_monitor()


# Settings.


@pytest.mark.parametrize(("rate", "line"), [(2400, "S1"), (9600, "S2"), (10400, "S3"), (19200, "S4")])
def test_set_baud_rate(card, fw, rate, line):
    card.set_baud_rate(rate)
    assert fw.baud == rate
    assert fw.port.lines == [line]


def test_set_baud_rate_while_open_is_rejected(card):
    card.open_channel()
    with pytest.raises(CommandError):
        card.set_baud_rate(9600)


def test_set_baud_rate_rejects_unsupported_rates(card, fw):
    with pytest.raises(ValueError, match="4800"):
        card.set_baud_rate(4800)
    assert fw.port.lines == []


def test_set_checksum(card, fw):
    card.set_checksum(Checksum.CLASSIC)
    assert fw.checksum == "classic"
    card.set_checksum(Checksum.ENHANCED)
    assert fw.checksum == "enhanced"


def test_set_diagnostic_classic(card, fw):
    card.set_diagnostic_classic(False)
    assert not fw.diag_classic
    card.set_diagnostic_classic(True)
    assert fw.diag_classic


# Device.


def test_version_is_cached(card, fw):
    assert card.version() == Version(hardware=3, firmware=6)
    assert card.firmware_version == 6
    assert card.version() == Version(3, 6)
    assert fw.port.lines == ["V"]


@pytest.mark.parametrize(("firmware", "supported"), [(5, False), (6, True), (7, True)])
def test_supports_response_queue(firmware, supported):
    fw = FakeFirmware(firmware=firmware)
    with UsbLinCard(transport=fw.port) as card:
        assert card.supports_response_queue is supported


def test_serial_number(card, fw):
    assert card.serial_number() == fw.uid


def test_read_flags(card, fw):
    fw.flags = 0x84
    assert card.read_flags() == ErrorFlags.LIN_FRAME_ERROR | ErrorFlags.NOISE
    assert card.read_flags() == ErrorFlags.NONE


def test_stack_usage():
    fw = FakeFirmware(debug=True)
    with UsbLinCard(transport=fw.port) as card:
        assert card.stack_usage() == 550


def test_stack_usage_needs_a_debug_build(card):
    with pytest.raises(CommandError):
        card.stack_usage()


def test_reboot_to_dfu_closes(card, fw):
    card.reboot_to_dfu()
    assert fw.booted
    assert card.closed
