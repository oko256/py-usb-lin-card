import subprocess
import sys
import threading

import pytest

from usblincard import CardInfo, ErrorFlags, UsbLinCard, UsbLinCardError, __version__, cli


@pytest.fixture
def opened(monkeypatch, fw):
    """Makes the CLI open a card on the fake firmware; records the arguments it was opened with."""
    calls = []

    def fake_open(args):
        calls.append((args.port, args.serial, args.timeout))
        return UsbLinCard(transport=fw.port, name="fake", timeout=args.timeout)

    monkeypatch.setattr(cli, "_open", fake_open)
    return calls


def test_list(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "find_cards",
        lambda: [CardInfo("/dev/ttyACM0", "1A2B3C4D5E01", "/dev/serial/by-id/x"), CardInfo("COM3", None)],
    )
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "1A2B3C4D5E01   /dev/ttyACM0   /dev/serial/by-id/x",
        "?              COM3",
    ]


def test_list_none(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_cards", list)
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "No USB LIN Cards found\n"


def test_info(opened, fw, capsys):
    fw.flags = 0x84 | 0x02
    assert cli.main(["info", "-s", "1A2B3C4D5E03"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Card:          fake",
        "Hardware:      3",
        "Firmware:      6",
        f"Serial number: {fw.uid}",
        "Error flags:   NOISE, LIN_FRAME_ERROR, 0x02",
    ]
    assert opened == [(None, "1A2B3C4D5E03", 2.0)]


def test_info_without_flags(opened, capsys):
    assert cli.main(["info", "-p", "/dev/ttyACM0", "--timeout", "0.5"]) == 0
    assert "Error flags:   none" in capsys.readouterr().out
    assert opened == [("/dev/ttyACM0", None, 0.5)]


def test_serial_and_port_exclude_each_other(capsys):
    with pytest.raises(SystemExit):
        cli.main(["info", "-s", "X", "-p", "Y"])
    assert "not allowed with" in capsys.readouterr().err


def test_monitor_count(opened, fw, capsys):
    fw.flags = 0x80
    threading.Timer(0.1, lambda: (fw.report(0x10, b"\xde\xad"), fw.report(0x11, b""))).start()
    assert cli.main(["monitor", "-b", "9600", "--classic", "-n", "2"]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("  0x10 [2] DE AD")
    assert lines[1].endswith("  0x11 [0] (no response)")
    assert float(lines[0].split()[0]) >= 0
    assert "Monitoring at 9600 baud" in err
    assert "Error flags" not in err  # the flags before the monitoring were cleared by the reset
    assert (fw.mode, fw.baud, fw.checksum) == ("closed", 9600, "classic")


def test_monitor_duration_and_flags(opened, fw, capsys):
    threading.Timer(0.05, lambda: setattr(fw, "flags", 0x04)).start()
    assert cli.main(["monitor", "-d", "0.2"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error flags: NOISE" in err
    assert fw.mode == "closed"


def test_monitor_stops_on_ctrl_c(opened, fw, monkeypatch):
    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(UsbLinCard, "receive", interrupt)
    assert cli.main(["monitor"]) == 0
    assert fw.mode == "closed"


def test_send(opened, fw, capsys, monkeypatch):
    original = UsbLinCard.send_frame

    def send_and_echo(card, lin_id, data, timeout=1.0):
        original(card, lin_id, data, timeout)
        fw.report(lin_id, data)  # what the card sees on the bus in monitoring mode

    monkeypatch.setattr(UsbLinCard, "send_frame", send_and_echo)
    assert cli.main(["send", "0x10", "DE", "AD", "0xBE,", "ef"]) == 0
    assert capsys.readouterr().out == "0x10 [4] DE AD BE EF\n"
    assert fw.single_frames == [(0x10, b"\xde\xad\xbe\xef")]
    assert fw.mode == "closed"


def test_send_not_seen(opened, fw, capsys, monkeypatch):
    monkeypatch.setattr(UsbLinCard, "receive", lambda *_args: None)
    assert cli.main(["send", "3c", "0102"]) == 1
    assert "wasn't seen on the bus" in capsys.readouterr().err
    assert fw.single_frames == [(0x3C, b"\x01\x02")]


def test_send_bad_data(opened, capsys):
    assert cli.main(["send", "10", "XY"]) == 1
    assert "not hexadecimal bytes: 'XY'" in capsys.readouterr().err


def test_send_too_long(opened, capsys):
    assert cli.main(["send", "10", "00" * 9]) == 1
    assert "payload length 9 out of range" in capsys.readouterr().err


def test_send_bad_id(capsys):
    with pytest.raises(SystemExit):
        cli.main(["send", "zz", "00"])
    assert "not a hexadecimal number: 'zz'" in capsys.readouterr().err


def test_raw(opened, capsys):
    assert cli.main(["raw", "V", "F"]) == 0
    assert capsys.readouterr().out.splitlines() == ["V -> 'V0306' ok", "F -> 'F00' ok"]


def test_raw_failure(opened, capsys):
    assert cli.main(["raw", "Q", "V"]) == 1
    assert capsys.readouterr().out.splitlines()[0] == "Q -> '' BEL"


def test_library_errors_are_printed(monkeypatch, capsys):
    def fail(_args):
        raise UsbLinCardError("no USB LIN Card is connected")

    monkeypatch.setattr(cli, "_open", fail)
    assert cli.main(["info"]) == 1
    assert capsys.readouterr().err == "usblincard: no USB LIN Card is connected\n"


def test_open_by_port_and_by_serial(monkeypatch):
    opened = []

    class FakeCard:
        def __init__(self, port, timeout):
            opened.append(("port", port, timeout))

        @classmethod
        def find(cls, serial, timeout):
            opened.append(("find", serial, timeout))

    monkeypatch.setattr(cli, "UsbLinCard", FakeCard)
    cli._open(cli._parser().parse_args(["info", "-p", "/dev/x"]))
    cli._open(cli._parser().parse_args(["info", "-s", "ABC", "--timeout", "1"]))
    cli._open(cli._parser().parse_args(["info"]))
    assert opened == [("port", "/dev/x", 2.0), ("find", "ABC", 1.0), ("find", None, 2.0)]


def test_flag_names():
    assert cli._flag_names(ErrorFlags.NONE) == "none"
    assert cli._flag_names(ErrorFlags(0xFF)) == (
        "RX_OVERRUN, NOISE, DATA_OVERRUN, USB_TX_OVERRUN, LIN_TX_FAILED, LIN_FRAME_ERROR, 0x12"
    )


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == f"usblincard {__version__}\n"


def test_python_m():
    out = subprocess.run(
        [sys.executable, "-m", "usblincard", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert out == f"usblincard {__version__}\n"
