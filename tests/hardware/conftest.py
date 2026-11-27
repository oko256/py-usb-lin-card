"""Hardware tests: USB LIN Cards with the card firmware, all on one powered LIN bus.

They are skipped unless the cards are given, as ports or USB serial numbers:

    pytest tests/hardware --card=1A2B3C4D5E03 --card2=1A2B3C4D5E01 --card3=1A2B3C4D5E02

(or USBLINCARD_CARD, USBLINCARD_CARD2 and USBLINCARD_CARD3). One card is enough for the tests of a
single card, two for most, and three for the monitoring tests. Each card is reset before and after
each test, and must have no error flags after it unless the test is marked expect_flags.
"""

import os
from pathlib import Path

import pytest

from usblincard import UsbLinCard, find_card


def pytest_configure(config):
    config.addinivalue_line("markers", "expect_flags: the test makes the cards latch error flags")


@pytest.fixture
def bench(request):
    """The cards opened for the test, in the order they were opened.

    At the end, all their channels are closed before any of the flags are read, card_a first:
    otherwise a commander still running after its responder has stopped latches "no response"
    errors.
    """
    cards: list[UsbLinCard] = []
    yield cards
    for card in cards:
        card.close_channel()
    latched = []
    for card in cards:
        flags = card.reset()
        card.close()
        if flags:
            latched.append(f"{card.name}: {flags!r}")
    if request.node.get_closest_marker("expect_flags") is None:
        assert latched == [], "the cards latched error flags"


def _open(request, bench, n: str) -> UsbLinCard:
    value = request.config.getoption(f"--card{n}") or os.environ.get(f"USBLINCARD_CARD{n}")
    if not value:
        pytest.skip(f"needs --card{n} (or USBLINCARD_CARD{n})")
    port = value if Path(value).exists() else find_card(value).path
    card = UsbLinCard(port, name=f"card{n or '1'}")
    bench.append(card)
    card.reset()
    return card


@pytest.fixture
def card_a(request, bench):
    """Card 1 (--card); the commander in the tests, so that it is closed first."""
    return _open(request, bench, "")


@pytest.fixture
def card_b(request, bench):
    """Card 2 (--card2)."""
    return _open(request, bench, "2")


@pytest.fixture
def card_c(request, bench):
    """Card 3 (--card3)."""
    return _open(request, bench, "3")
