"""Cross-site request forgery guard for the web UI.

relocarr has no login yet (the Settings section's "UI login" is still planned), so without
this any web page the operator happens to open could submit the UI's forms to relocarr
through their browser -- the browser is on the LAN, so it reaches the UI fine -- and e.g.
change a target folder or trigger a Redownload, which deletes a file in Radarr. Browsers
always attach Origin (or at least Referer) to a cross-site POST, so a state-changing request
is rejected when either header names a different host than the one it was sent to. Requests
with neither header (curl, scripts) aren't browser-originated and pass through unchanged.
"""

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Authenticated by the per-app X-Relocarr-Token header instead (and called by the *arr
# apps themselves, never by a browser).
EXEMPT_PREFIXES = ("/webhook/",)


def is_cross_site(headers) -> bool:
    # X-Forwarded-Host covers a reverse proxy that rewrites Host; a cross-site form can't
    # set custom headers, so accepting it doesn't open a hole.
    own_hosts = {h.lower() for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    origin = headers.get("origin")
    if origin is not None:
        return origin == "null" or urlsplit(origin).netloc.lower() not in own_hosts
    referer = headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() not in own_hosts
    return False


async def csrf_guard(request: Request, call_next):
    if (
        request.method not in SAFE_METHODS
        and not request.url.path.startswith(EXEMPT_PREFIXES)
        and is_cross_site(request.headers)
    ):
        return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)
