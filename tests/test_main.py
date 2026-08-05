from pathlib import Path
from unittest.mock import patch

import aiosqlite
from httpx import ASGITransport, AsyncClient


async def test_index_html_served(tmp_path: Path) -> None:
    """Test that GET / serves the HTML with CSS vars injected."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><!-- CONFIG_VARS --></html>")

    import src.main as main_mod
    from src.auth.session import SESSION_COOKIE, sign_session
    from src.config import settings

    with (
        patch.object(main_mod, "_static_dir", static_dir),
        patch.object(main_mod, "_index_path", static_dir / "index.html"),
    ):
        from src.main import app

        # Force rebuild of HTML (since we patched the path) and store in app.state
        app.state.html = main_mod._build_html()

        token = sign_session(settings.auth_secret_key)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            headers={"x-forwarded-proto": "https"},
            cookies={SESSION_COOKIE: token},
        ) as c:
            resp = await c.get("/")

    assert resp.status_code == 200
    assert "<style>" in resp.text
    assert "--feed-initial-count" in resp.text


async def test_build_html_real_index() -> None:
    """_build_html works with the real index.html if it exists."""
    import src.main as main_mod

    # Use real paths (index.html exists in src/static/)
    result = main_mod._build_html()
    assert "--feed-initial-count" in result
    assert "--ui-debug" in result
    assert "<style>" in result


async def test_injected_config_wins_over_stylesheet_defaults() -> None:
    """The injected <style> must come after style.css, or the env is ignored.

    Both set the same custom properties on :root with identical specificity,
    so the later declaration wins. With the injection first, style.css's
    defaults silently overrode UI_DEBUG, FEED_INITIAL_COUNT and
    IMAGE_AUTOSCROLL_DELAY_S -- the values were served, then immediately
    reset, and readConfig() only ever saw the defaults.
    """
    import src.main as main_mod

    result = main_mod._build_html()
    assert result.index("style.css") < result.index("--ui-debug"), (
        "injected CSS variables must appear after the style.css link"
    )


async def test_env_values_reach_the_injected_css(monkeypatch: object) -> None:
    """A non-default setting must actually show up in the served HTML."""
    import src.main as main_mod
    from src.config import settings

    monkeypatch.setattr(settings, "ui_debug", 1)  # type: ignore[attr-defined]
    monkeypatch.setattr(settings, "feed_initial_count", 42)  # type: ignore[attr-defined]

    result = main_mod._build_html()
    assert "--ui-debug:1;" in result
    assert "--feed-initial-count:42;" in result


async def test_api_items_requires_a_session(db: aiosqlite.Connection) -> None:
    """The API test app in conftest builds a bare FastAPI() with the four
    routers and RequestIDMiddleware — production wraps every route in
    AuthMiddleware. All 60+ API tests therefore exercise a request shape
    production rejects, and deleting add_middleware(AuthMiddleware) from
    main.py left the whole suite green, including /api/media/proxy, the route
    that makes outbound fetches on the caller's behalf.
    """
    from src.auth.session import SESSION_COOKIE, sign_session
    from src.config import settings
    from src.db.connection import get_db
    from src.main import app

    async def _override_db() -> aiosqlite.Connection:
        return db

    app.dependency_overrides[get_db] = _override_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            headers={"x-forwarded-proto": "https"},
            follow_redirects=False,
        ) as c:
            anonymous = await c.get("/api/items")
            assert anonymous.status_code == 302, "no cookie must not reach the router"
            assert anonymous.headers["location"].startswith("/login")

            c.cookies.set(SESSION_COOKIE, sign_session(settings.auth_secret_key))
            authed = await c.get("/api/items")
            assert authed.status_code == 200
            assert authed.json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)
