"""Sphinx configuration: the reference is generated from the docstrings (Google style)."""

import usblincard

project = "usb-lin-card"
author = "Olli Kokko"
copyright = "2026, Olli Kokko"
release = version = usblincard.__version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]
exclude_patterns = ["_build"]

# Warn about every reference that doesn't resolve (sphinx-build -n makes these errors with -W).
nitpicky = True

# Show "UsbLinCard", not "usblincard.card.UsbLinCard"; the package re-exports the public names.
add_module_names = False

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_class_signature = "separated"
autoclass_content = "class"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_ivar = True

myst_heading_anchors = 3

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "serial": ("https://pyserial.readthedocs.io/en/latest", None),
}

html_theme = "furo"
html_title = f"usb-lin-card {release}"
