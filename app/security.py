"""Guard for the routes that act on hardware or change measurements: bridge control, board
flashing and calibration. Reading data stays open, as it is everywhere else in telem."""

from urllib.parse import urlparse

from fastapi import HTTPException, Request

from .config import settings

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def control(request: Request):
    """With RECEIVER_TOKEN set, require it. Otherwise allow only this computer, and only the
    dashboard itself: a web page in your browser can reach localhost too, so a request that
    names a different Origin is refused."""
    if settings.receiver_token:
        if request.headers.get("authorization") != f"Bearer {settings.receiver_token}":
            raise HTTPException(401, "This control needs the access token (RECEIVER_TOKEN).")
        return
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        raise HTTPException(
            403, "Controls only accept requests from this computer. Set RECEIVER_TOKEN to allow others."
        )
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin request refused.")
