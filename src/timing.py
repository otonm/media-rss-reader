"""Duration measurement for the log lines at src/api boundaries.

The perf_counter / x1000 / {:.1f}ms block was copy-pasted nine times across
four modules, with seven variable names and two precisions, and the copies had
already drifted: one computed its duration after the `async with`, so a 404
exited untimed, while another computed it before the 404 check. Reading the
clock at the log site removes that choice.
"""

import time
from collections.abc import Callable


def timer() -> Callable[[], float]:
    """Return a callable giving milliseconds elapsed since this call."""
    t0 = time.perf_counter()
    return lambda: (time.perf_counter() - t0) * 1000
