"""The library against real cards on a LIN bus (see conftest.py). card_a is usually the commander,
card_b a responder and card_c a monitor."""

import re
import time

import pytest

from usblincard import Checksum, CommandError, ErrorFlags, auto_slot_ms

pytestmark = pytest.mark.hardware


def _period_ms(frames) -> float:
    """Average time between the frames, in milliseconds."""
    return (frames[-1].timestamp - frames[0].timestamp) * 1000 / (len(frames) - 1)


# One card.


def test_identity(card_a):
    version = card_a.version()
    assert version.firmware >= 6
    assert card_a.supports_response_queue
    assert re.fullmatch(r"[0-9A-F]{24}", card_a.serial_number())
    assert card_a.read_flags() == ErrorFlags.NONE


def test_rejected_commands(card_a):
    card_a.open_channel()
    with pytest.raises(CommandError):
        card_a.set_baud_rate(9600)
    with pytest.raises(CommandError):
        card_a.open_monitor()
    with pytest.raises(CommandError):
        card_a.replace_response(0x10, b"\x00")  # not published
    assert card_a.close_channel()
    assert not card_a.close_channel()
    with pytest.raises(CommandError):
        card_a.send_frame(0x10, b"\x00", timeout=0.05)  # closed


def test_table_and_queue_limits(card_a):
    card_a.publish(0x10, b"\x00")
    for i in range(16):
        assert card_a.queue_response(0x10, bytes([i]))
    assert not card_a.queue_response(0x10, b"\xff")
    for i in range(31):
        card_a.subscribe(0x20 + i % 0x10, 1)
    with pytest.raises(CommandError):
        card_a.subscribe(0x3F, 1)


def test_own_single_frame_is_seen_in_monitoring_mode(card_a):
    card_a.open_monitor()
    card_a.send_frame(0x05, b"\xca\xfe")
    frame = card_a.receive(1)
    assert (frame.id, frame.data) == (0x05, b"\xca\xfe")


# Two cards.


def test_commander_polls_a_responder(card_a, card_b):
    card_b.publish(0x10, b"\xde\xad\xbe\xef")
    card_b.open_channel()
    card_a.schedule_header(0x10, 4, slot_ms=20)
    card_a.open_channel()
    frames = card_a.wait_frames(30, timeout=3)
    assert {(f.id, f.data) for f in frames} == {(0x10, b"\xde\xad\xbe\xef")}
    assert _period_ms(frames) == pytest.approx(20, rel=0.05)
    # A responder doesn't report its own publishes (the response isn't changed with t2/t3).
    assert card_b.frames() == []


def test_automatic_slot_time(card_a, card_b):
    card_b.publish(0x11, bytes(range(8)))
    card_b.open_channel()
    card_a.schedule_header(0x11, 8)
    card_a.open_channel()
    frames = card_a.wait_frames(100, timeout=5)
    assert _period_ms(frames) == pytest.approx(auto_slot_ms(8), rel=0.05)


def test_command_frame_reaches_a_subscriber(card_a, card_b):
    card_b.subscribe(0x20, 2)
    card_b.open_channel()
    card_a.schedule_frame(0x20, b"\xca\xfe", 20)
    card_a.open_channel()
    frames = card_b.wait_frames(10, timeout=2)
    assert {(f.id, f.data) for f in frames} == {(0x20, b"\xca\xfe")}
    assert card_a.frames() == []


def test_single_frame_reaches_a_subscriber(card_a, card_b):
    card_b.subscribe(0x21, 3)
    card_b.open_channel()
    card_a.open_channel()
    for i in range(5):
        card_a.send_frame(0x21, bytes([i, i, i]))
    assert [f.data[0] for f in card_b.wait_frames(5, timeout=2)] == [0, 1, 2, 3, 4]


def test_response_queue(card_a, card_b):
    card_b.publish(0x10, b"\x00\x00")
    card_b.open_channel()
    for i in range(1, 6):
        assert card_b.queue_response(0x10, bytes([0, i]))
    card_a.schedule_header(0x10, 2, slot_ms=20)
    card_a.open_channel()
    expected = [bytes([0, i]) for i in (0, 1, 2, 3, 4, 5, 5, 5)]
    assert [f.data for f in card_a.wait_frames(8, timeout=2)[:8]] == expected
    # The publishes are reported, in the same order.
    assert [f.data for f in card_b.wait_frames(8, timeout=2)[:8]] == expected


def test_replace_response(card_a, card_b):
    card_b.publish(0x10, b"\xaa")
    card_b.open_channel()
    card_a.schedule_header(0x10, 1, slot_ms=20)
    card_a.open_channel()
    card_a.wait_frames(3, timeout=1)
    card_b.replace_response(0x10, b"\xbb")
    time.sleep(0.1)
    card_a.clear_frames()
    assert {f.data for f in card_a.wait_frames(5, timeout=1)} == {b"\xbb"}
    assert {f.data for f in card_b.wait_frames(3, timeout=1)} == {b"\xbb"}


@pytest.mark.expect_flags
def test_request(card_a, card_b):
    card_b.publish(0x30, bytes(range(8)))
    card_b.open_channel()
    card_a.open_channel()
    assert card_a.request(0x30, 8, timeout=1) == bytes(range(8))
    assert card_a.request(0x30, 8, timeout=1) == bytes(range(8))
    # Nobody responds to 0x31: no response, and the commander latches a frame error.
    assert card_a.request(0x31, 8, timeout=0.2) is None
    assert ErrorFlags.LIN_FRAME_ERROR in card_a.read_flags()


@pytest.mark.parametrize("baud", [2400, 9600, 10400, 19200])
def test_baud_rates(card_a, card_b, baud):
    for card in (card_a, card_b):
        card.reset(baud_rate=baud)
    card_b.publish(0x12, b"\x12\x34")
    card_b.open_channel()
    card_a.schedule_header(0x12, 2)
    card_a.open_channel()
    frames = card_a.wait_frames(10, timeout=3)
    assert {f.data for f in frames} == {b"\x12\x34"}
    assert _period_ms(frames) == pytest.approx(auto_slot_ms(2, baud), rel=0.1)


@pytest.mark.expect_flags
def test_checksum_models_must_match(card_a, card_b):
    card_b.subscribe(0x22, 1)
    card_b.open_channel()
    card_a.set_checksum(Checksum.CLASSIC)
    card_a.open_channel()
    card_a.send_frame(0x22, b"\x5a")
    time.sleep(0.1)
    assert card_b.frames() == []
    assert ErrorFlags.LIN_FRAME_ERROR in card_b.read_flags()
    card_b.set_checksum(Checksum.CLASSIC)
    card_a.send_frame(0x22, b"\x5a")
    assert card_b.wait_frame(timeout=1).data == b"\x5a"


# Three cards.


@pytest.mark.expect_flags
def test_monitor_sees_both_sides(card_a, card_b, card_c):
    card_c.open_monitor()
    card_b.publish(0x13, b"\x01")
    card_b.open_channel()
    card_a.schedule_header(0x13, 1, slot_ms=20)
    card_a.schedule_header(0x14, 1, slot_ms=20)  # nobody responds
    card_a.open_channel()
    frames = card_c.wait_frames(20, timeout=2)
    seen = {(f.id, f.data) for f in frames}
    assert seen == {(0x13, b"\x01"), (0x14, b"")}
    assert any(f.header_only for f in frames)


def test_monitor_sees_single_frames(card_a, card_c):
    card_c.open_monitor()
    card_a.open_monitor()
    card_a.send_frame(0x3C, bytes(8))  # a diagnostic frame: classic checksum on both
    frame = card_c.wait_frame(timeout=1)
    assert (frame.id, frame.data) == (0x3C, bytes(8))
