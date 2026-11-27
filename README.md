# Python library for the USB LIN Card

`usb-lin-card` controls a [USB LIN Card](https://github.com/oko256/usb-lin-card-hw) running the
[USB LIN Card firmware](https://github.com/oko256/usb-lin-card-fw) from Python. A script can use
the card as a LIN commander, as a responder, or as a bus monitor. The package also has a
`usblincard` command for quick work on the bench.

It needs Python 3.10 or later and pyserial. Changing published responses (`replace_response()`,
`queue_response()`) needs card firmware 6 or later.

This README has a [tutorial](#tutorial-first-script), [how-to guides](#how-to-guides), the
[reference](#reference) (generated from the docstrings), and [explanations](#explanation) of how
the library works.

## Tutorial: first script

Install the library into a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install "usb-lin-card @ git+https://github.com/oko256/py-usb-lin-card.git@v1.0.0"
```

Plug in a card and check that it is found:

```console
$ .venv/bin/usblincard list
1A2B3C4D5E03   /dev/ttyACM0   /dev/serial/by-id/usb-oko256_USB_LIN_Card_1A2B3C4D5E03-if00
```

Save this as `first.py`. It opens the card, prints its versions, and then prints every frame on
the bus until the bus has been quiet for 10 seconds:

```python
from usblincard import UsbLinCard

with UsbLinCard.find() as card:  # the only card that is connected
    print(card.version())
    card.reset()  # closed, empty tables, 19200 baud
    card.open_monitor()
    for frame in card.iter_frames(timeout=10):
        print(frame)
    card.reset()
```

Run it with `.venv/bin/python first.py` while there is traffic on the bus (e.g. from another card
running `usblincard send 10 DE AD BE EF`):

```console
Version(hardware=3, firmware=6)
0x10 [4] DE AD BE EF
```

## How-to guides

### Choose a card

```python
from usblincard import UsbLinCard, find_cards

print(find_cards())  # serial numbers and ports of the connected cards
card = UsbLinCard.find("1A2B3C4D5E03")  # by USB serial number, whichever port it got
card = UsbLinCard("/dev/ttyACM0")  # by port (or COM3 on Windows)
```

### Poll a responder as the commander

```python
card.reset()
card.schedule_header(0x10, 4, slot_ms=50)  # the header of 0x10 every 50 ms, 4-byte response
card.open_channel()
for frame in card.iter_frames(timeout=1):  # until no response has come for 1 s
    print(frame.id, frame.data.hex(), frame.timestamp)
```

To get one response only, use `card.request(0x10, 4)` with the channel open. It returns the
response, or None if nobody responded. It wipes the tables while doing it.

### Act as a responder

```python
card.reset()
card.publish(0x10, b"\xde\xad\xbe\xef")  # answer the header of 0x10
card.subscribe(0x20, 2)  # report the 2-byte frames of 0x20
card.open_channel()
frame = card.receive(timeout=5)  # None if none came
```

Send a command frame (header and data) with `card.schedule_frame(0x20, b"\xca\xfe", 20)` in the
schedule, or once with `card.send_frame(0x20, b"\xca\xfe")`. A single frame needs the channel
open, either normally or in monitoring mode.

### Publish a sequence of responses

Queue the responses. Each publish (to the headers of another commander) is then reported, which
frees a place in the queue for the next response:

```python
responses = [i.to_bytes(2, "big") for i in range(100)]
card.reset()
card.publish(0x10, responses[0])
card.open_channel()
queued = 1
while queued < len(responses) and card.queue_response(0x10, responses[queued]):  # fill the queue
    queued += 1
for frame in card.iter_frames(timeout=1):
    print("published", frame.data.hex())
    if queued < len(responses) and card.queue_response(0x10, responses[queued]):
        queued += 1
```

`replace_response()` changes the response right away instead, and clears the queue.

### Check for bus errors

```python
flags = card.read_flags()  # reading clears them
if flags:
    print(f"errors: {flags!r}")  # e.g. <ErrorFlags.NOISE|LIN_FRAME_ERROR: 132>
```

### Use another baud rate or the classic checksum

```python
from usblincard import Checksum

card.reset(baud_rate=9600, checksum=Checksum.CLASSIC)
```

The baud rate can only be changed while the channel is closed; `reset()` closes it.

### Handle errors

All the errors of the library derive from `UsbLinCardError`. A command the card rejects raises
`CommandError`, and a missing reply raises `ReplyTimeout` (also a `TimeoutError`). Arguments that
the card would reject raise `ValueError` before anything is sent.

### Test a script without a card

Give the card a `transport` instead of a port: any object with `read()`, `write()`, `flush()`,
`close()` and `in_waiting` (see `usblincard.transport.SerialLike`). The transport plays the
firmware's part: it gets the command lines, and returns the replies and frame lines.

```python
card = UsbLinCard(transport=my_fake_port)
```

### Use the command-line tool

```bash
usblincard list                             # the connected cards
usblincard info -s 1A2B3C4D5E03             # versions, serial number and error flags
usblincard monitor --baud 9600              # print the frames on the bus until Ctrl-C
usblincard send 3C 01 02 03 04 05 06 07 08  # send one frame
usblincard raw V F                          # send firmware commands and print the replies
```

`monitor` and `send` reset the card first, and leave it closed.

### Develop the library

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,docs]'
.venv/bin/ruff check && .venv/bin/ruff format --check
.venv/bin/pytest --cov                      # unit tests, at least 95 % branch coverage
.venv/bin/pytest tests/hardware --card=1A2B3C4D5E03 --card2=1A2B3C4D5E01 --card3=1A2B3C4D5E02
.venv/bin/sphinx-build -W --keep-going -n docs docs/_build/html
```

The unit tests run against an emulation of the firmware (`tests/fake.py`). The hardware tests need
one to three cards with the card firmware on one powered LIN bus. The cards can be given as ports
or as USB serial numbers (or as `USBLINCARD_CARD`, `USBLINCARD_CARD2` and `USBLINCARD_CARD3`), and
the tests are skipped without them. The first card is the commander, and it needs the master
pull-up.

## Reference

The API reference is generated from the docstrings with Sphinx. Build it with the last command
above, and open `docs/_build/html/index.html`. Or read it with `help()`, e.g.
`help(usblincard.UsbLinCard)`.

- `usblincard.card`: `UsbLinCard`, one card and all its commands.
- `usblincard.discovery`: `find_cards()`, `find_card()` and `CardInfo`.
- `usblincard.protocol`: `Frame`, `Reply`, `Version`, `BaudRate`, `Checksum`, `ErrorFlags`, and
  the encoders, parsers and constants of the firmware protocol.
- `usblincard.errors`: the exceptions.
- `usblincard.transport`: `SerialLike`, the interface of a transport.
- `usblincard.cli`: the `usblincard` command (also `python -m usblincard`).

The package itself re-exports all the classes, functions and exceptions above.

## Explanation

### Replies and frames

The card answers each command with exactly one reply, terminated with CR (success) or BEL (failure).
It reports LIN frames whenever they happen, as lines starting with `t`, and no reply starts with
`t`. A reader thread reads the port all the time and sorts the lines into a reply queue and a frame
buffer. So frames are never lost while a command waits for its reply, and the frames wait in the
buffer until the script takes them.

### Timestamps

`Frame.timestamp` is the `time.monotonic()` time when the frame's line arrived from USB, not the
time on the bus. USB adds a few milliseconds of jitter. Averages over many frames are accurate,
though.

### One command at a time

Commands are sent one at a time under a lock, each waiting for its reply, so threads can share a
card. If a reply times out, it may still arrive later. Then the next command resyncs first: it sends
`V` and drops the replies up to the `V` reply. That way, a late reply isn't taken as the reply of
another command.

### Tables

The schedule and the responder table hold 32 entries each. Changes take effect immediately, and the
schedule restarts from its first entry. Entries can't be removed one by one, only all together
(`wipe()`). That's why `request()` schedules its header and then wipes the tables: the card has no
command for sending a single header.

### Single frames

`send_frame()` sends a header and its response once. The card rejects a new single frame while it is
still sending the previous one, so `send_frame()` retries until the card accepts it or the timeout
passes.

### State

The card keeps its mode, settings and tables while it is powered, also after the port is closed. A
script that starts with `reset()` doesn't depend on what ran before it. A script that ends with
`reset()` leaves the bus quiet: an open card keeps answering headers by itself.

### Firmware versions

`version()` is read once and cached. Features that need a newer firmware raise `UnsupportedError`
instead of an obscure `CommandError`; check `supports_response_queue` to choose another way.
