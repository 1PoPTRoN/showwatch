"""Run the fake site: `python -m tests.fakesite [--port N]` (or `make fakesite`).

It always binds 127.0.0.1. Port 0 picks a free port. Once listening it prints
`FAKESITE_PORT=<port>`, which the test fixture reads.
"""

import argparse
import socket
import sys

import uvicorn

from tests.fakesite.app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FakeShow: a local fake cinema site for tests")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (0 = any free port)")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))  # loopback only, always
    port = sock.getsockname()[1]
    print(f"FAKESITE_PORT={port}", flush=True)
    print(f"FakeShow running at http://127.0.0.1:{port}/movie (Ctrl+C to stop)", file=sys.stderr)

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
    )

    def stop() -> None:
        server.should_exit = True

    app.state.shutdown = stop
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
