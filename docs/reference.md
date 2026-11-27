# API reference

Generated from the docstrings. Everything most scripts need can be imported from the package
itself, e.g. `from usblincard import UsbLinCard, Frame, ErrorFlags`: the card, finding cards, the
errors, and the frame, reply, version, setting and flag types of the protocol.

## The card

```{eval-rst}
.. automodule:: usblincard.card
   :no-members:

.. autoclass:: usblincard.card.UsbLinCard
   :members:
   :special-members: __init__
```

## Finding cards

```{eval-rst}
.. automodule:: usblincard.discovery
   :members:
```

## Errors

```{eval-rst}
.. automodule:: usblincard.errors
   :members:
```

## The protocol

```{eval-rst}
.. automodule:: usblincard.protocol
   :members:
   :exclude-members: __new__
```

## Transports

```{eval-rst}
.. automodule:: usblincard.transport
   :members:
```
