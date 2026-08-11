"""Duration measurement for the log lines at src/api boundaries.

The clock is read at the log site, so the measured span always covers the
call it reports.
"""

import time
from collections.abc import Callable


def timer() -> Callable[[], float]:
    """Return a callable giving milliseconds elapsed since this call."""
    t0 = time.perf_counter()
    return lambda: (time.perf_counter() - t0) * 1000
