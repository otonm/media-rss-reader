from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient


async def test_index_html_served(tmp_path: Path) -> None:
    """Test that GET / serves the HTML with CSS vars injected."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<html><!-- SLIDESHOW_TRANSITION --></html>"
    )

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

        token = sign_session(settings.auth_secret_key.get_secret_value())
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            headers={"x-forwarded-proto": "https"},
            cookies={SESSION_COOKIE: token},
        ) as c:
            resp = await c.get("/")

    assert resp.status_code == 200
    assert "<style>" in resp.text
    assert "--slideshow-transition-ms" in resp.text


async def test_build_html_missing_file() -> None:
    """_build_html returns empty string when index.html does not exist."""
    import src.main as main_mod

    with patch.object(main_mod, "_index_path", Path("/nonexistent/index.html")):
        result = main_mod._build_html()

    assert result == ""


async def test_build_html_real_index() -> None:
    """_build_html works with the real index.html if it exists."""
    import src.main as main_mod

    # Use real paths (index.html exists in src/static/)
    result = main_mod._build_html()
    assert "--slideshow-transition-ms" in result
    assert "<style>" in result
