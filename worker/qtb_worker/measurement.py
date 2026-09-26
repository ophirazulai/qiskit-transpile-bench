"""Optional coordinator handshakes around measured work, outside the timed calls."""

import os
from contextlib import contextmanager


@contextmanager
def measured():
    descriptor = os.environ.get("QTB_MEASUREMENT_FD")
    if descriptor is None:
        yield
        return
    fd = int(descriptor)

    def boundary(event):
        os.write(fd, event)
        if os.read(fd, 1) != event:
            raise RuntimeError("Measurement observer disconnected")

    boundary(b"B")
    try:
        yield
    finally:
        boundary(b"E")
