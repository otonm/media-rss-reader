import time

from src.timing import timer


def test_timer_reports_elapsed_milliseconds() -> None:
    elapsed = timer()
    time.sleep(0.02)
    ms = elapsed()
    assert 15 < ms < 500, f"expected ~20ms, got {ms}"


def test_timer_can_be_read_more_than_once_and_advances() -> None:
    """media.py computed db_ms after its `async with`, so a 404 exited untimed,
    while items.py computed it before its 404 check. Reading the clock at the
    log site removes the choice."""
    elapsed = timer()
    first = elapsed()
    time.sleep(0.01)
    assert elapsed() > first


def test_timer_survives_an_exception_path() -> None:
    elapsed = timer()
    try:
        raise ValueError("boom")
    except ValueError:
        assert elapsed() >= 0
