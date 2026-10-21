from __future__ import annotations

import httpx

from .base import Torrent

_FIELDS = ["id", "name", "hashString", "percentDone", "status", "downloadDir"]


class TransmissionClient:
    """Transmission RPC client.

    Behavior confirmed live against Transmission 6.0.1 during spike testing,
    2026-10-21:
    - The 409 session-id handshake is required on every fresh client; the
      session id is cached and only re-fetched on a 409.
    - `torrent-stop`/`torrent-remove` with no `ids` key does NOT reliably
      apply to all torrents -- always pass explicit ids.
    - `hashString` comes back lowercase; the *arr's `downloadId` for the same
      torrent is uppercase. Compare case-insensitively.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        url_base: str = "/transmission/",
        timeout: float = 10.0,
    ) -> None:
        auth = (username, password) if username else None
        self._client = httpx.Client(
            base_url=f"http://{host}:{port}{url_base}rpc",
            auth=auth,
            timeout=timeout,
        )
        self._session_id: str | None = None

    def _call(self, method: str, arguments: dict | None = None) -> dict:
        payload = {"method": method, "arguments": arguments or {}}
        headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
        resp = self._client.post("", json=payload, headers=headers)
        if resp.status_code == 409:
            self._session_id = resp.headers["X-Transmission-Session-Id"]
            headers = {"X-Transmission-Session-Id": self._session_id}
            resp = self._client.post("", json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if body.get("result") != "success":
            raise RuntimeError(f"transmission RPC {method} failed: {body.get('result')}")
        return body["arguments"]

    def list_torrents(self) -> list[Torrent]:
        args = self._call("torrent-get", {"fields": _FIELDS})
        return [
            Torrent(
                id=t["id"],
                name=t["name"],
                hash=t["hashString"],
                percent_done=t["percentDone"],
                status=t["status"],
                download_dir=t["downloadDir"],
            )
            for t in args["torrents"]
        ]

    def stop(self, torrent_id: int) -> None:
        self._call("torrent-stop", {"ids": [torrent_id]})

    def remove(self, torrent_id: int, delete_data: bool) -> None:
        self._call(
            "torrent-remove",
            {"ids": [torrent_id], "delete-local-data": delete_data},
        )

    def close(self) -> None:
        self._client.close()
