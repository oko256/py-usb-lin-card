from dataclasses import dataclass

import pytest

import usblincard.card
from fake import FakeSerial
from usblincard import CardInfo, UsbLinCard, UsbLinCardError, discovery, find_card, find_cards


@dataclass
class Port:
    device: str
    serial_number: str | None
    vid: int | None = 0x1209
    pid: int | None = 0xBEBE


@pytest.fixture
def ports(monkeypatch, tmp_path):
    """Fake ports: two cards (one with a by-id link), an STLINK and a port without USB IDs."""
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in ("ttyACM0", "ttyACM1", "ttyACM2", "ttyS0"):
        (dev / name).touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-oko256_USB_LIN_Card_1A2B3C4D5E03-if00").symlink_to(dev / "ttyACM2")
    ports = [
        Port(str(dev / "ttyACM2"), "1A2B3C4D5E03"),
        Port(str(dev / "ttyACM1"), "001122334455667788990011", vid=0x0483, pid=0x3754),
        Port(str(dev / "ttyACM0"), "1A2B3C4D5E01"),
        Port(str(dev / "ttyS0"), None, vid=None, pid=None),
    ]
    monkeypatch.setattr(discovery.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(discovery, "_BY_ID_DIR", by_id)
    return dev, by_id, ports


def test_find_cards(ports):
    dev, by_id, _ = ports
    assert find_cards() == [
        CardInfo(str(dev / "ttyACM0"), "1A2B3C4D5E01", None),
        CardInfo(str(dev / "ttyACM2"), "1A2B3C4D5E03", str(by_id / "usb-oko256_USB_LIN_Card_1A2B3C4D5E03-if00")),
    ]


def test_path_prefers_the_by_id_link(ports):
    dev, by_id, _ = ports
    first, second = find_cards()
    assert first.path == str(dev / "ttyACM0")
    assert second.path == str(by_id / "usb-oko256_USB_LIN_Card_1A2B3C4D5E03-if00")


def test_find_cards_without_by_id_dir(ports, monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "_BY_ID_DIR", tmp_path / "missing")
    assert [c.by_id_path for c in find_cards()] == [None, None]


def test_find_cards_none(monkeypatch):
    monkeypatch.setattr(discovery.list_ports, "comports", list)
    assert find_cards() == []
    with pytest.raises(UsbLinCardError, match="no USB LIN Card is connected"):
        find_card()


def test_find_card_by_serial_number(ports):
    assert find_card("1a2b3c4d5e03").serial_number == "1A2B3C4D5E03"
    with pytest.raises(UsbLinCardError, match="no USB LIN Card with serial number 123"):
        find_card("123")


def test_find_card_needs_a_choice_between_several(ports):
    with pytest.raises(UsbLinCardError, match=r"several .*: 1A2B3C4D5E01 \(.*ttyACM0\), 1A2B3C4D5E03"):
        find_card()


def test_find_card_finds_the_only_one(ports):
    _, _, port_list = ports
    del port_list[2]
    assert find_card().serial_number == "1A2B3C4D5E03"


def test_open_found_card(ports, monkeypatch):
    _, by_id, _ = ports
    opened = []

    def fake_serial(port, *_args, **_kwargs):
        opened.append(port)
        return FakeSerial()

    monkeypatch.setattr(usblincard.card.serial, "Serial", fake_serial)
    with UsbLinCard.find("1A2B3C4D5E03", timeout=0.5) as card:
        assert card.name == "1A2B3C4D5E03"
        assert card.timeout == 0.5
    assert opened == [str(by_id / "usb-oko256_USB_LIN_Card_1A2B3C4D5E03-if00")]


def test_open_found_card_without_a_serial_number(monkeypatch):
    monkeypatch.setattr(discovery.list_ports, "comports", lambda: [Port("/dev/ttyACM5", None)])
    monkeypatch.setattr(usblincard.card.serial, "Serial", lambda *_a, **_k: FakeSerial())
    with UsbLinCard.find() as card:
        assert card.name == "/dev/ttyACM5"
