import threading
import time

import pytest

from fake import FakeFirmware, wait_until
from usblincard import (
    Checksum,
    CommandError,
    ErrorFlags,
    Frame,
    ProtocolError,
    ReplyTimeout,
    UnsupportedError,
    UsbLinCard,
)

# Tables.


def test_schedule_header(card, fw):
    card.schedule_header(0x10, 4, slot_ms=50)
    card.schedule_header(0x11, 8)
    assert fw.port.lines == ["R010000324", "r0118"]
    assert fw.schedule == [(0x10, 50), (0x11, 13)]
    assert [(e["id"], e["publish"], e["length"]) for e in fw.responder] == [(0x10, False, 4), (0x11, False, 8)]


def test_schedule_frame(card, fw):
    card.schedule_frame(0x21, b"\x01\x02", 13)
    assert fw.port.lines == ["T0210000D20102"]
    assert fw.responder[0]["data"] == b"\x01\x02"


def test_subscribe_and_publish(card, fw):
    card.subscribe(0x20, 2)
    card.publish(0x10, b"\xde\xad\xbe\xef")
    assert fw.port.lines == ["r1202", "t1104DEADBEEF"]


def test_full_table_is_rejected(card):
    for i in range(32):
        card.subscribe(i, 1)
    with pytest.raises(CommandError):
        card.publish(0x3F, b"\x00")
    with pytest.raises(CommandError):
        card.schedule_header(0x3F, 1)


def test_bad_arguments_are_not_sent(card, fw):
    with pytest.raises(ValueError, match="LIN ID"):
        card.subscribe(0x40, 1)
    with pytest.raises(ValueError, match="payload length"):
        card.publish(1, bytes(9))
    with pytest.raises(ValueError, match="slot time"):
        card.schedule_frame(1, b"\x00", 0)
    assert fw.port.lines == []


def test_wipe(card, fw):
    card.schedule_header(0x10, 4)
    card.wipe()
    assert fw.schedule == []
    assert fw.responder == []


# Response queue.


def test_replace_response(card, fw):
    card.publish(0x10, b"\x00\x00")
    card.replace_response(0x10, b"\x00\x01")
    assert fw.responder[0]["data"] == b"\x00\x01"
    fw.published(0x10)
    assert card.receive(1).data == b"\x00\x01"


def test_replace_response_of_an_unpublished_id_is_rejected(card):
    card.subscribe(0x10, 2)
    with pytest.raises(CommandError):
        card.replace_response(0x10, b"\x00\x01")
    with pytest.raises(CommandError):
        card.replace_response(0x11, b"\x00\x01")


def test_queue_response(card, fw):
    card.publish(0x10, b"\x00\x00")
    assert card.queue_response(0x10, b"\x00\x01")
    assert card.queue_response(0x10, b"\x00\x02")
    for _ in range(3):
        fw.published(0x10)
    assert [f.data for f in card.wait_frames(3, 1)] == [b"\x00\x00", b"\x00\x01", b"\x00\x02"]


def test_queue_response_full(card):
    card.publish(0x10, b"\x00")
    card.publish(0x11, b"\x00")
    for i in range(16):
        assert card.queue_response(0x10, bytes([i]))
    assert not card.queue_response(0x10, b"\xff")
    card.replace_response(0x10, b"\x00")  # clears the queue
    assert card.queue_response(0x10, b"\x01")
    assert not card.queue_response(0x11, b"\x01")  # holds 0x10's responses


def test_response_queue_needs_firmware_6():
    fw = FakeFirmware(firmware=5)
    with UsbLinCard(transport=fw.port) as card:
        card.publish(0x10, b"\x00")
        with pytest.raises(UnsupportedError, match="needs firmware 6 or later \\(the card has 5\\)"):
            card.replace_response(0x10, b"\x01")
        with pytest.raises(UnsupportedError):
            card.queue_response(0x10, b"\x01")
        assert not any(line.startswith(("t2", "t3")) for line in fw.port.lines)


# Single frames.


def test_send_frame(card, fw):
    card.open_monitor()
    card.send_frame(0x05, b"\xca\xfe")
    assert fw.single_frames == [(0x05, b"\xca\xfe")]


def test_send_frame_retries_while_busy(card, fw):
    card.open_channel()
    fw.busy = 3
    card.send_frame(0x05, b"\x01")
    assert fw.port.lines.count("t0051" + "01") == 4
    assert fw.single_frames == [(0x05, b"\x01")]


def test_send_frame_gives_up(card, fw):
    start = time.monotonic()
    with pytest.raises(CommandError, match="t005110"):
        card.send_frame(0x05, b"\x10", timeout=0.05)  # the channel is closed
    assert time.monotonic() - start < 1
    assert len(fw.port.lines) > 1


# Received frames.


def test_receive(card, fw):
    assert card.receive(0) is None
    fw.report(0x10, b"\x01")
    fw.report(0x11, b"")
    frame = card.receive(1)
    assert (frame.id, frame.data) == (0x10, b"\x01")
    assert card.receive(1).header_only
    assert card.receive(0.01) is None


def test_receive_uses_the_default_timeout(fw):
    with UsbLinCard(transport=fw.port, timeout=0.05) as card:
        start = time.monotonic()
        assert card.receive() is None
        assert 0.04 <= time.monotonic() - start < 1


def test_receive_raises_protocol_errors(card, fw):
    fw.port.feed(b"t01\r")
    fw.report(0x10, b"\x01")
    assert wait_until(lambda: card.frames())
    with pytest.raises(ProtocolError):
        card.receive(0)
    assert card.receive(0).id == 0x10


def test_frames_wait_and_clear(card, fw):
    fw.report(0x10, b"\x01")
    fw.report(0x11, b"\x02")
    assert [f.id for f in card.wait_frames(2, 1)] == [0x10, 0x11]
    assert card.wait_frame().id == 0x10
    assert len(card.frames()) == 2
    card.clear_frames()
    assert card.frames() == []
    with pytest.raises(ReplyTimeout, match="got 0/1 frames"):
        card.wait_frame(timeout=0.02)


def test_collect_frames(card, fw):
    fw.report(0x01, b"\x01")
    time.sleep(0.05)
    threading.Timer(0.02, fw.report, [0x02, b"\x02"]).start()
    assert [f.id for f in card.collect_frames(0.2)] == [0x02]


def test_iter_frames_stops_after_a_quiet_timeout(card, fw):
    for i in range(3):
        fw.report(i, bytes([i]))
    assert [f.id for f in card.iter_frames(timeout=0.1)] == [0, 1, 2]


def test_iter_frames_without_a_timeout_keeps_waiting(card, fw):
    threading.Timer(1.2, fw.report, [0x07, b"\x07"]).start()
    frames = card.iter_frames()
    assert next(frames).id == 0x07


def test_frames_are_frame_objects(card, fw):
    fw.report(0x3F, b"\x01\x02")
    frame = card.receive(1)
    assert isinstance(frame, Frame)
    assert frame.line == "t03F20102"


# Helpers.


def test_reset(card, fw):
    card.set_baud_rate(9600)
    card.set_checksum(Checksum.CLASSIC)
    card.set_diagnostic_classic(False)
    card.publish(0x10, b"\x00")
    card.open_channel()
    fw.flags = 0x80
    fw.report(0x10, b"\x00")
    card.wait_frame()
    card.send_raw(b"V\r")
    assert card.reset() == ErrorFlags.LIN_FRAME_ERROR
    assert (fw.mode, fw.baud, fw.checksum, fw.diag_classic) == ("closed", 19200, "enhanced", True)
    assert fw.responder == []
    assert card.frames() == []
    assert card.pending_replies() == []


def test_reset_to_other_settings(card, fw):
    assert card.reset(baud_rate=10400, checksum=Checksum.CLASSIC) == ErrorFlags.NONE
    assert (fw.baud, fw.checksum) == (10400, "classic")


def test_request(card, fw):
    card.open_channel()
    fw.report(0x30, b"\xff" * 8)  # an old frame: dropped

    schedules = []

    def respond():
        schedules.append(list(fw.schedule))
        fw.report(0x31, b"\x00")  # another frame
        fw.report(0x30, b"\x01\x02")  # wrong length
        fw.report(0x30, bytes(range(8)))

    card.subscribe(0x31, 1)
    time.sleep(0.05)
    threading.Timer(0.05, respond).start()
    assert card.request(0x30, 8, timeout=2) == bytes(range(8))
    assert fw.schedule == []
    assert fw.responder == []
    assert schedules == [[(0x30, 20)]]
    assert fw.port.lines[-3:] == ["w", "R030000148", "w"]


def test_request_timeout(card, fw):
    card.open_channel()
    assert card.request(0x30, 8, timeout=0.05, slot_ms=10) is None
    assert fw.port.lines[-3:] == ["w", "R0300000A8", "w"]
    assert fw.schedule == []
