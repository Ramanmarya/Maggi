"""
Dashboard server — read-only view of the bot on localhost.

    python3 -m dashboard.server            live state
    python3 -m dashboard.server --demo     sample positions, for layout work
    python3 -m dashboard.server --port 8787

Binds 127.0.0.1 only. This process never writes bot state and has no control
endpoints: changing the kill switch or phase goes through scripts/status.py,
so there stays exactly one auditable path to trading state.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import load
from .render import render


def make_handler(demo: bool):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = render(load(demo=demo)).encode()
            except Exception as e:  # surface errors in the page, don't 500 silently
                body = f"<pre>dashboard error: {type(e).__name__}: {e}</pre>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the terminal clean; real logging is in logs/events.jsonl

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="Maggi QQQ dashboard")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--demo", action="store_true", help="render sample positions instead of live state")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.demo))
    print(f"Maggi dashboard → http://127.0.0.1:{args.port}{'  (SAMPLE DATA)' if args.demo else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
