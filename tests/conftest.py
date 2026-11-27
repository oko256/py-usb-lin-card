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


def pytest_addoption(parser):
    # The options of the hardware tests (tests/hardware); here, since only the root conftest can add them.
    group = parser.getgroup("usblincard hardware tests")
    for n in ("", "2", "3"):
        group.addoption(
            f"--card{n}",
            default=None,
            help=f"USB LIN Card {n or '1'} for the hardware tests: a port or a USB serial number "
            f"(env USBLINCARD_CARD{n})",
        )
