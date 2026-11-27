import re

import usblincard


def test_version_is_semantic():
    assert re.fullmatch(r"\d+\.\d+\.\d+", usblincard.__version__)
