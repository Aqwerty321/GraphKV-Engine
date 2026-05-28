#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import socket
import struct
import subprocess
from pathlib import Path


def make_client_frame(payload: bytes) -> bytes:
    header = bytearray([0x82])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def read_frame(sock: socket.socket) -> bytes:
    first = sock.recv(2)
    if len(first) != 2:
        raise RuntimeError("server closed before sending a frame")
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    mask = b""
    if first[1] & 0x80:
        mask = sock.recv(4)
    payload = bytearray()
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise RuntimeError("server closed while sending a frame")
        payload.extend(chunk)
    if mask:
        payload = bytearray(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(payload)


def zstd_compress_file(path: Path) -> bytes:
    try:
        import zstandard as zstd
    except ImportError:
        zstd = None

    if zstd is not None:
        return zstd.ZstdCompressor(write_content_size=True).compress(path.read_bytes())

    try:
        completed = subprocess.run(["zstd", "-q", "-c", str(path)], check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("send_docs_ws.py requires either the Python 'zstandard' package or the 'zstd' CLI") from exc
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Zstd-compress and send a GraphKV document NDJSON bundle to /v1/ingest_docs.")
    parser.add_argument("path", type=Path, help="path to a normalized document NDJSON bundle")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    payload = zstd_compress_file(args.path)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /v1/ingest_docs HTTP/1.1\r\n"
        f"Host: {args.host}:{args.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")

    with socket.create_connection((args.host, args.port), timeout=10) as sock:
        sock.sendall(request)
        response = sock.recv(4096).decode("iso-8859-1")
        if " 101 " not in response or expected_accept not in response:
            raise RuntimeError(f"websocket handshake failed:\n{response}")
        sock.sendall(make_client_frame(payload))
        print(read_frame(sock).decode("utf-8", errors="replace"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())