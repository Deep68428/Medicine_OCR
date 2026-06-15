#!/usr/bin/env python3
"""
test_conveyor_trigger.py
Mock conveyor server that machine_code connects to.
Listens on localhost:5000, accepts one connection, then sends 't' on Enter.

Usage:
    python test_conveyor_trigger.py                  # listen on localhost:5000
    python test_conveyor_trigger.py --port 5001      # custom port

In machine_code/.env set:
    conveyor_ip = 127.0.0.1  (or configure via backend machine config)

Then start machine_code, run this script, press Enter to send a trigger.
"""

import argparse
import socket

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000


def main():
    parser = argparse.ArgumentParser(
        description="Mock conveyor server — sends 't' trigger on Enter"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (default: %(default)s)"
    )
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="Bind port (default: %(default)s)",
    )
    args = parser.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        print(
            f"Listening on {args.host}:{args.port} — waiting for machine_code to connect..."
        )

        conn, addr = server.accept()
        print(f"machine_code connected from {addr}")
        print("Press Enter to send 't' trigger. Ctrl+C to quit.\n")

        with conn:
            while True:
                try:
                    inp = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if inp == "t":
                    conn.sendall(b"t")
                else:
                    conn.sendall(inp.encode())
                print("Sent trigger 't'")


if __name__ == "__main__":
    main()
