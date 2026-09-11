"""Local static server with a same-origin proxy for the public DPS API."""

from __future__ import annotations

import argparse
import http.server
import pathlib
import urllib.error
import urllib.request


PUBLIC_API_PREFIX = "/api/v1/dps/public/"
PUBLIC_API_ORIGIN = "https://daodaogame.vip"


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith(PUBLIC_API_PREFIX):
            self._proxy_public_api()
            return
        super().do_GET()

    def _proxy_public_api(self) -> None:
        request = urllib.request.Request(
            f"{PUBLIC_API_ORIGIN}{self.path}",
            headers={"Accept": "application/json", "User-Agent": "gmzz-site-dev/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get(
                    "Content-Type", "application/json; charset=utf-8"
                )
        except urllib.error.HTTPError as error:
            payload = error.read()
            status = error.code
            content_type = error.headers.get(
                "Content-Type", "application/json; charset=utf-8"
            )
        except (OSError, urllib.error.URLError):
            payload = b'{"ok":false,"error":"upstream_unavailable"}'
            status = 502
            content_type = "application/json; charset=utf-8"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8087)
    args = parser.parse_args()
    site_root = pathlib.Path(__file__).resolve().parent
    handler = lambda *handler_args, **handler_kwargs: SiteHandler(  # noqa: E731
        *handler_args, directory=str(site_root), **handler_kwargs
    )
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"GMZZ site: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
