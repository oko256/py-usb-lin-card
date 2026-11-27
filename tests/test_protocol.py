import pytest

from usblincard import protocol as p
from usblincard.errors import CommandError, ProtocolError, ReplyTimeout, UnsupportedError, UsbLinCardError

# Encoders.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (p.t0(0x10, b"\xde\xad\xbe\xef"), "t0104DEADBEEF"),
        (p.t1(0x3F, b"\x01"), "t13F101"),
        (p.t2(0x00, bytes(8)), "t20080000000000000000"),
        (p.t3(0x05, b"\xca\xfe"), "t3052CAFE"),
        (p.r0(0x10, 4), "r0104"),
        (p.r1(0x20, 8), "r1208"),
        (p.R0(0x10, 50, 4), "R0100003" + "24"),
        (p.R0(0x01, p.SLOT_MS_MAX, 1), "R001FFFFF1"),
        (p.T0(0x21, 13, b"\x01\x02"), "T021" + "0000D" + "20102"),
        (p.set_baud_rate(2400), "S1"),
        (p.set_baud_rate(9600), "S2"),
        (p.set_baud_rate(p.BaudRate.B10400), "S3"),
        (p.set_baud_rate(19200), "S4"),
        (p.set_checksum(p.Checksum.ENHANCED), "x1"),
        (p.set_checksum(p.Checksum.CLASSIC), "x2"),
        (p.set_diagnostic_classic(True), "x3"),
        (p.set_diagnostic_classic(False), "x4"),
    ],
)
def test_encoders(line, expected):
    assert line == expected


def test_encoders_accept_bytes_like_data():
    assert p.t0(1, bytearray(b"\x01")) == "t0011" + "01"
    assert p.t1(1, memoryview(b"\xab")) == "t1011AB"


def test_longest_commands_fit_in_a_line():
    # The CR counts too.
    assert len(p.T0(p.LIN_ID_MAX, p.SLOT_MS_MAX, bytes(8))) + 1 <= p.LINE_MAX
    assert len(p.t3(p.LIN_ID_MAX, bytes(8))) + 1 <= p.LINE_MAX


@pytest.mark.parametrize("lin_id", [-1, 0x40, 0xFF])
@pytest.mark.parametrize(
    "encode",
    [
        lambda i: p.t0(i, b"\x00"),
        lambda i: p.t1(i, b"\x00"),
        lambda i: p.t2(i, b"\x00"),
        lambda i: p.t3(i, b"\x00"),
        lambda i: p.r0(i, 1),
        lambda i: p.r1(i, 1),
        lambda i: p.R0(i, 10, 1),
        lambda i: p.T0(i, 10, b"\x00"),
    ],
)
def test_encoders_reject_bad_ids(encode, lin_id):
    with pytest.raises(ValueError, match="LIN ID"):
        encode(lin_id)


@pytest.mark.parametrize("length", [0, 9])
@pytest.mark.parametrize(
    "encode",
    [
        lambda n: p.t0(1, bytes(n)),
        lambda n: p.t1(1, bytes(n)),
        lambda n: p.t2(1, bytes(n)),
        lambda n: p.t3(1, bytes(n)),
        lambda n: p.r0(1, n),
        lambda n: p.r1(1, n),
        lambda n: p.R0(1, 10, n),
        lambda n: p.T0(1, 10, bytes(n)),
    ],
)
def test_encoders_reject_bad_lengths(encode, length):
    with pytest.raises(ValueError, match="payload length"):
        encode(length)


@pytest.mark.parametrize("slot_ms", [0, -5, p.SLOT_MS_MAX + 1])
def test_encoders_reject_bad_slots(slot_ms):
    with pytest.raises(ValueError, match="slot time"):
        p.R0(1, slot_ms, 1)
    with pytest.raises(ValueError, match="slot time"):
        p.T0(1, slot_ms, b"\x00")


@pytest.mark.parametrize("rate", [0, 4800, 20000])
def test_set_baud_rate_rejects_unsupported_rates(rate):
    with pytest.raises(ValueError, match=r"2400, 9600, 10400, 19200"):
        p.set_baud_rate(rate)


def test_baud_rate_presets():
    assert [r.preset for r in p.BaudRate] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("length", "baud", "expected"),
    [(1, 19200, 6), (8, 19200, 13), (8, 2400, 104), (4, 9600, 18), (0, 1000, 88)],
)
def test_auto_slot_ms(length, baud, expected):
    assert p.auto_slot_ms(length, baud) == expected


def test_auto_slot_ms_defaults_to_19200():
    assert p.auto_slot_ms(8) == 13


# Frames.


def test_parse_frame():
    frame = p.parse_frame("t0104DEADBEEF", 12.5)
    assert frame == p.Frame(0x10, b"\xde\xad\xbe\xef", 12.5)
    assert not frame.header_only
    assert frame.line == "t0104DEADBEEF"
    assert str(frame) == "0x10 [4] DE AD BE EF"


def test_parse_frame_lower_case():
    assert p.parse_frame("t03f2cafe").data == b"\xca\xfe"


def test_parse_header_only_frame():
    frame = p.parse_frame("t0050")
    assert frame == p.Frame(0x05, b"")
    assert frame.header_only
    assert frame.line == "t0050"
    assert str(frame) == "0x05 [0] (no response)"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "t0",
        "t010",
        "t1104DEADBEEF",
        "x0104DEADBEEF",
        "t0104DEADBE",  # too short for its length
        "t0102DEADBEEF",  # too long for its length
        "t0104DEADBEE",  # odd number of digits
        "t0404DEADBEEF",  # ID out of range
        "t0G04DEADBEEF",
        "t0104DEADBEEG",
        "t0102 AB CD",
        "t0+12ABCD",
    ],
)
def test_parse_frame_rejects_malformed_lines(line):
    with pytest.raises(ProtocolError, match="malformed frame line"):
        p.parse_frame(line)


# Replies.


def test_parse_version():
    assert p.parse_version("V0306") == p.Version(hardware=3, firmware=6)
    assert p.parse_version("Vff0a") == p.Version(hardware=255, firmware=10)


def test_parse_serial_number():
    assert p.parse_serial_number("N" + "0123456789abcdef01234567") == "0123456789ABCDEF01234567"


def test_parse_flags():
    assert p.parse_flags("F00") == p.ErrorFlags.NONE
    assert p.parse_flags("F84") == p.ErrorFlags.LIN_FRAME_ERROR | p.ErrorFlags.NOISE
    # Reserved bits are kept.
    assert int(p.parse_flags("F12")) == 0x12


def test_parse_stack_usage():
    assert p.parse_stack_usage("d00000226") == 550


@pytest.mark.parametrize(
    ("parse", "text"),
    [
        (p.parse_version, ""),
        (p.parse_version, "V030"),
        (p.parse_version, "V03060"),
        (p.parse_version, "X0306"),
        (p.parse_version, "V03G6"),
        (p.parse_version, "V+306"),
        (p.parse_serial_number, "N0123"),
        (p.parse_serial_number, "N" + "Z" * 24),
        (p.parse_flags, "F0"),
        (p.parse_flags, "G00"),
        (p.parse_flags, "F0x"),
        (p.parse_stack_usage, "d226"),
    ],
)
def test_reply_parsers_reject_malformed_replies(parse, text):
    with pytest.raises(ProtocolError, match="malformed"):
        parse(text)


def test_constants_match_the_firmware():
    assert (p.CR, p.BEL) == (0x0D, 0x07)
    assert (p.TABLE_SIZE, p.RESPONSE_QUEUE_DEPTH, p.LINE_MAX) == (32, 16, 32)
    assert [int(f) for f in p.ErrorFlags if f] == [0x01, 0x04, 0x08, 0x20, 0x40, 0x80]


# Errors.


def test_error_hierarchy():
    for cls in (CommandError, ReplyTimeout, ProtocolError, UnsupportedError):
        assert issubclass(cls, UsbLinCardError)
    assert issubclass(ReplyTimeout, TimeoutError)


def test_command_error():
    reply = p.Reply("", ok=False)
    error = CommandError("O", reply, card="adapter")
    assert str(error) == "adapter: command 'O' was rejected by the card"
    assert (error.command, error.reply) == ("O", reply)
    assert str(CommandError("C", reply)) == "command 'C' was rejected by the card"
