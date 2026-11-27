import pytest

from fake import FakeFirmware
from usblincard import UsbLinCard


@pytest.fixture
def fw():
    return FakeFirmware()


@pytest.fixture
def card(fw):
    card = UsbLinCard(transport=fw.port, name="card", timeout=0.5)
    yield card
    card.close()
