import threading
import time

import pytest

from fake import FakeSerial
from usblincard import protocol as p
from usblincard._reader import _MAX_LINE, LineReader
from usblincard.errors import ProtocolError, ReplyTimeout, UsbLinCardError


@pytest.fixture(params=[None, 1, 3], ids=["whole", "chunk1", "chunk3"])
def port(request):
    return FakeSerial(chunk=request.param)


@pytest.fixture
def reader(port):
    reader = LineReader(port, "card")
    yield reader
    reader.close()


def test_replies_and_frames_are_split(port, reader):
    port.feed(b"V0306\rt0104DEADBEEF\r\x07t0050\r\rF00\r")
    assert reader.reply(1) == p.Reply("V0306", ok=True)
    assert reader.reply(1) == p.Reply("", ok=False)
    assert reader.reply(1) == p.Reply("", ok=True)
    assert reader.reply(1) == p.Reply("F00", ok=True)
    frames = reader.wait_frames(2, 1)
    assert [(f.id, f.data) for f in frames] == [(0x10, b"\xde\xad\xbe\xef"), (0x05, b"")]


def test_frames_are_timestamped_on_arrival(port, reader):
    before = time.monotonic()
    port.feed(b"t0011AB\r")
    frame = reader.pop_frame(1)
    assert before <= frame.timestamp <= time.monotonic()


def test_reply_timeout(reader):
    with pytest.raises(ReplyTimeout, match=r"card: no reply within 0\.05 s"):
        reader.reply(0.05)


def test_reply_waits_for_a_late_reply(port, reader):
    threading.Timer(0.05, port.feed, [b"\r"]).start()
    assert reader.reply(2).ok


def test_drain_replies(port, reader):
    port.feed(b"\r\x07")
    deadline = time.monotonic() + 1
    while len(reader._replies) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert reader.drain_replies() == [p.Reply("", True), p.Reply("", False)]
    assert reader.drain_replies() == []


def test_pop_frame(port, reader):
    assert reader.pop_frame(0.02) is None
    port.feed(b"t0011AA\rt0011BB\r")
    assert reader.pop_frame(1).data == b"\xaa"
    assert reader.pop_frame(1).data == b"\xbb"
    assert reader.pop_frame(0) is None


def test_frames_snapshot_and_clear(port, reader):
    port.feed(b"t0011AA\rt0021BB\r")
    assert len(reader.wait_frames(2, 1)) == 2
    assert [f.id for f in reader.frames()] == [1, 2]
    assert len(reader.frames()) == 2  # not removed
    reader.clear_frames()
    assert reader.frames() == []


def test_wait_frames_timeout_lists_what_came(port, reader):
    port.feed(b"t0011AA\r")
    with pytest.raises(ReplyTimeout, match=r"got 1/2 frames within 0.1 s: \['t0011AA'\]"):
        reader.wait_frames(2, 0.1)


def test_malformed_frame_is_dropped_and_raised_once(port, reader):
    port.feed(b"t0104DE\rt0011AA\r")
    assert reader.pop_frame(1).data == b"\xaa"
    with pytest.raises(ProtocolError, match="card: malformed frame line 't0104DE'"):
        reader.check()
    reader.check()


def test_overlong_line_is_dropped(port, reader):
    port.feed(b"x" * (_MAX_LINE + 1) + b"V0306\r")
    # The garbage is dropped once it gets too long, and the reader goes on with the next line.
    reply = reader.reply(1)
    assert reply.text == "V0306"
    with pytest.raises(ProtocolError, match="without a terminator"):
        reader.check()


def test_non_ascii_bytes_are_replaced(port, reader):
    port.feed(b"\xff\r")
    assert reader.reply(1).text == "�"


def test_read_error_is_fatal(port, reader):
    port.fail(OSError("device disconnected"))
    with pytest.raises(UsbLinCardError, match="reading from the card failed: device disconnected") as e:
        reader.reply(1)
    assert isinstance(e.value.__cause__, OSError)
    with pytest.raises(UsbLinCardError):
        reader.check()
    with pytest.raises(UsbLinCardError):
        reader.pop_frame(1)


def test_waiter_is_woken_by_a_read_error(port, reader):
    threading.Timer(0.05, port.fail, [OSError("gone")]).start()
    start = time.monotonic()
    with pytest.raises(UsbLinCardError, match="gone"):
        reader.wait_frames(1, 5)
    assert time.monotonic() - start < 2


def test_buffered_items_are_still_returned_after_a_read_error(port, reader):
    port.feed(b"t0011AA\r")
    reader.wait_frames(1, 1)
    port.fail(OSError("gone"))
    assert reader.pop_frame(1).data == b"\xaa"


def test_close_stops_the_thread_and_closes_the_port():
    port = FakeSerial()
    reader = LineReader(port, "card")
    reader.close()
    assert port.closed
    assert not reader._thread.is_alive()


def test_read_error_while_closing_is_ignored():
    port = FakeSerial()
    reader = LineReader(port, "card")
    reader._stop.set()
    port.fail(OSError("closed under the reader"))
    reader._thread.join(1)
    assert reader._fatal is None
    reader.close()
