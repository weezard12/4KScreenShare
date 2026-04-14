from __future__ import annotations

import argparse

from aiohttp import web

from screenshare.network.signaling import create_relay_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the public signaling relay for 4K Screen Share.")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind. Default: 0.0.0.0")
    parser.add_argument("--port", default=8080, type=int, help="Port to bind. Default: 8080")
    args = parser.parse_args()
    web.run_app(create_relay_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
