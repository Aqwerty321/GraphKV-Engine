#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run(command: list[str], *, cwd: Path | None = None, stdout_path: Path | None = None) -> str:
    if stdout_path:
        with stdout_path.open("w") as output:
            completed = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True, stdout=output, stderr=subprocess.STDOUT)
        if completed.returncode != 0:
            raise RuntimeError(f"command failed with exit {completed.returncode}; see {stdout_path}: {' '.join(command)}")
        return ""

    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def docker_logs(container: str, tail: int = 160) -> str:
    completed = subprocess.run(["docker", "logs", "--tail", str(tail), container], check=False, text=True, capture_output=True)
    return (completed.stdout + completed.stderr).strip()


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray([0x80 | opcode])
    mask_bit = 0x80
    if len(payload) < 126:
        header.append(mask_bit | len(payload))
    elif len(payload) <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", len(payload)))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", len(payload)))
    mask = os.urandom(4)
    header.extend(mask)
    header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header)


def recv_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    first, second = read_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    masked = bool(second & 0x80)
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]
    mask = read_exact(sock, 4) if masked else b""
    payload = bytearray(read_exact(sock, length))
    if masked:
        for index in range(length):
            payload[index] ^= mask[index % 4]
    return fin, opcode, bytes(payload)


def recv_text(sock: socket.socket) -> str:
    while True:
        fin, opcode, payload = recv_frame(sock)
        if opcode == 0x9:
            send_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x8:
            raise RuntimeError(f"server closed websocket: {payload!r}")
        if opcode != 0x1:
            raise RuntimeError(f"unexpected websocket opcode {opcode} while waiting for text")
        chunks = [payload]
        while not fin:
            fin, opcode, payload = recv_frame(sock)
            if opcode == 0x9:
                send_frame(sock, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError(f"server closed websocket: {payload!r}")
            if opcode != 0x0:
                raise RuntimeError(f"unexpected websocket opcode {opcode} in fragmented text")
            chunks.append(payload)
        return b"".join(chunks).decode("utf-8")


def websocket_connect(host: str, port: int, path: str) -> socket.socket:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    sock = socket.create_connection((host, port), timeout=5)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    headers = response.decode("iso-8859-1")
    if "101 Switching Protocols" not in headers or expected_accept not in headers:
        raise RuntimeError(headers)
    return sock


def summarize(samples_ms: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples_ms)
    p99_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.99) - 1))
    return {
        "runs": len(ordered),
        "mean_ms": statistics.fmean(ordered),
        "median_ms": statistics.median(ordered),
        "p99_ms": ordered[p99_index],
        "best_ms": min(ordered),
        "worst_ms": max(ordered),
    }


def parse_benchmark_output(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed


def zstd_compress_file(path: Path) -> bytes:
    try:
        import zstandard as zstd
    except ImportError:
        zstd = None

    if zstd is not None:
        payload = path.read_bytes()
        return zstd.ZstdCompressor(write_content_size=True).compress(payload)

    try:
        completed = subprocess.run(["zstd", "-q", "-c", str(path)], check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("network profiling requires either the Python 'zstandard' package or the 'zstd' CLI") from exc
    return completed.stdout


def binary_roundtrip_ms(sock: socket.socket, payload: bytes) -> float:
    started = time.perf_counter_ns()
    send_frame(sock, 0x2, payload)
    parsed = json.loads(recv_text(sock))
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    if not parsed.get("ok"):
        raise RuntimeError(parsed)
    return elapsed_ms


def persistent_binary_samples(host: str, port: int, payload: bytes, runs: int, path: str = "/v1/ingest_scip") -> list[float]:
    with websocket_connect(host, port, path) as sock:
        return [binary_roundtrip_ms(sock, payload) for _ in range(runs)]


def synthesize_once(host: str, port: int, query: str, session_id: str = "sess_profile") -> tuple[float, dict[str, Any]]:
    with websocket_connect(host, port, "/v1/ingest") as sock:
        send_frame(sock, 0x1, json.dumps({"type": "auth", "token": "dev", "tenant_id": 42}).encode("utf-8"))
        auth = json.loads(recv_text(sock))
        if not auth.get("ok"):
            raise RuntimeError(auth)
        started = time.perf_counter_ns()
        send_frame(sock, 0x1, json.dumps({
            "type": "synthesize",
            "session_id": session_id,
            "message_id": "msg_profile",
            "agent": "bench",
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4",
            "query": query,
        }).encode("utf-8"))
        parsed = json.loads(recv_text(sock))
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    if not parsed.get("ok"):
        raise RuntimeError(parsed)
    return elapsed_ms, parsed


def synthesis_text(parsed: dict[str, Any]) -> str:
    parts = parsed.get("opencode", {}).get("body", {}).get("parts", [])
    if not parts:
        return ""
    first = parts[0]
    if not isinstance(first, dict):
        return ""
    text = first.get("text", "")
    return text if isinstance(text, str) else ""


def run_core_benchmarks(image: str, root: Path, fixtures: list[Path], runs_per_fixture: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for fixture in fixtures:
        rows: list[dict[str, float | int]] = []
        for run_index in range(1, runs_per_fixture + 1):
            output = run([
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/opt/graphkv/bin/ingest_latency_benchmark",
                "-v",
                f"{fixture}:/fixture.scip:ro",
                image,
                "/fixture.scip",
            ])
            parsed = parse_benchmark_output(output)
            rows.append({
                "run": run_index,
                "bytes": int(parsed["bytes"]),
                "mean_us": float(parsed["mean_us"]),
                "median_us": float(parsed["median_us"]),
                "p99_us": float(parsed["p99_us"]),
                "checksum": int(parsed["checksum"]),
            })
        mean_values = [float(row["mean_us"]) for row in rows]
        p99_values = [float(row["p99_us"]) for row in rows]
        results.append({
            "fixture": str(fixture.relative_to(root)),
            "bytes": int(rows[0]["bytes"]),
            "runs": rows,
            "summary": {
                "mean_of_means_us": statistics.fmean(mean_values),
                "stdev_mean_us": statistics.stdev(mean_values) if len(mean_values) > 1 else 0.0,
                "best_mean_us": min(mean_values),
                "worst_mean_us": max(mean_values),
                "mean_of_p99_us": statistics.fmean(p99_values),
                "worst_p99_us": max(p99_values),
            },
        })
    return results


def run_warden_profile(image: str, records: int, runs_count: int) -> dict[str, Any]:
    output = run([
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/opt/graphkv/bin/warden_profile",
        image,
        "--records",
        str(records),
        "--runs",
        str(runs_count),
    ])
    return json.loads(output)


def run_network_benchmarks(
    host: str,
    port: int,
    fixtures: list[Path],
    doc_fixtures: list[Path],
    binary_runs: int,
    synthesize_runs: int,
    concurrency: int,
    concurrent_binary_runs: int,
    doc_synthesize_query: str,
    require_doc_retrieval_markers: bool,
) -> dict[str, Any]:
    binary: dict[str, Any] = {}
    for fixture in fixtures:
        uncompressed_size = fixture.stat().st_size
        payload = zstd_compress_file(fixture)
        samples = persistent_binary_samples(host, port, payload, binary_runs)
        binary[fixture.name] = {
            "bytes": uncompressed_size,
            "compressed_bytes": len(payload),
            "compression_ratio": len(payload) / uncompressed_size if uncompressed_size else 0.0,
            **summarize(samples),
        }

    docs: dict[str, Any] = {}
    for fixture in doc_fixtures:
        uncompressed_size = fixture.stat().st_size
        payload = zstd_compress_file(fixture)
        samples = persistent_binary_samples(host, port, payload, binary_runs, "/v1/ingest_docs")
        docs[fixture.name] = {
            "bytes": uncompressed_size,
            "compressed_bytes": len(payload),
            "compression_ratio": len(payload) / uncompressed_size if uncompressed_size else 0.0,
            **summarize(samples),
        }

    synthesize_samples: list[float] = []
    for _ in range(synthesize_runs):
        elapsed_ms, _ = synthesize_once(host, port, "why is ingest slow?", "sess_profile_loop")
        synthesize_samples.append(elapsed_ms)

    doc_retrieval_check: dict[str, Any] | None = None
    if doc_fixtures:
        elapsed_ms, parsed = synthesize_once(host, port, doc_synthesize_query, "sess_doc_retrieval_check")
        text = synthesis_text(parsed)
        marker_offset = text.find("doc_retrieval_backend:")
        excerpt = text[marker_offset:marker_offset + 1600] if marker_offset >= 0 else text[:1600]
        doc_retrieval_check = {
            "query": doc_synthesize_query,
            "latency_ms": elapsed_ms,
            "doc_backend_usearch": "doc_retrieval_backend: usearch-hnsw" in text,
            "doc_backend_present": "doc_retrieval_backend:" in text,
            "graph_overlap": "graph_overlap: yes" in text,
            "dense_sparse_graph": "retrieval: dense sparse graph" in text,
            "excerpt": excerpt,
        }
        if require_doc_retrieval_markers:
            required = ["doc_backend_usearch", "doc_backend_present", "graph_overlap", "dense_sparse_graph"]
            missing = [name for name in required if not doc_retrieval_check[name]]
            if missing:
                raise RuntimeError({"doc_retrieval_marker_check_failed": missing, "check": doc_retrieval_check})

    concurrent_binary: dict[str, Any] | None = None
    if concurrency > 0 and concurrent_binary_runs > 0:
        fixture = fixtures[-1]
        uncompressed_size = fixture.stat().st_size
        payload = zstd_compress_file(fixture)
        samples: list[float] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            pending = [executor.submit(persistent_binary_samples, host, port, payload, concurrent_binary_runs) for _ in range(concurrency)]
            for future in concurrent.futures.as_completed(pending):
                samples.extend(future.result())
        concurrent_binary = {
            "fixture": fixture.name,
            "bytes": uncompressed_size,
            "compressed_bytes": len(payload),
            "compression_ratio": len(payload) / uncompressed_size if uncompressed_size else 0.0,
            "concurrency": concurrency,
            **summarize(samples),
        }

    return {
        "binary": binary,
        "docs": docs,
        "synthesize": summarize(synthesize_samples),
        "doc_retrieval_check": doc_retrieval_check,
        "concurrent_binary": concurrent_binary,
    }


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description="Build, smoke, and profile the GraphKV Docker runtime path.")
    parser.add_argument("--image", default="graphkv-engine:e2e")
    parser.add_argument("--container", default="graphkv-e2e")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--build-log", type=Path, default=Path("/tmp/graphkv-docker-build.log"))
    parser.add_argument("--json-output", type=Path, default=root / "benchmarks/results/docker_e2e_latest.json")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--binary-runs", type=int, default=40)
    parser.add_argument("--synthesize-runs", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--concurrent-binary-runs", type=int, default=8)
    parser.add_argument("--container-start-timeout", type=float, default=60.0)
    parser.add_argument("--warden-profile-records", type=int, default=256)
    parser.add_argument("--warden-profile-runs", type=int, default=20)
    parser.add_argument("--skip-warden-profile", action="store_true")
    parser.add_argument("--doc-synthesize-query", default="browser-use WebSocket CDP URL controller session")
    parser.add_argument("--require-doc-retrieval-markers", action="store_true")
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--network-fixture", action="append", type=Path, default=[])
    parser.add_argument("--doc-fixture", action="append", type=Path, default=[])
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()

    fixtures = [path.resolve() for path in args.fixture] or [
        root / "benchmarks/data/python_send_scip_ws.scip",
        root / "benchmarks/data/python_repo.scip",
    ]
    network_fixtures = [path.resolve() for path in args.network_fixture] or [
        root / "benchmarks/data/index.scip",
        root / "benchmarks/data/python_repo.scip",
    ]
    doc_fixtures = [path.resolve() for path in args.doc_fixture]

    if not args.skip_build:
        run(["docker", "build", "-t", args.image, "."], cwd=root, stdout_path=args.build_log)

    subprocess.run(["docker", "rm", "-f", args.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    container_id = run(["docker", "run", "-d", "--rm", "--name", args.container, "-p", f"{args.port}:8888", args.image])
    try:
        deadline = time.monotonic() + args.container_start_timeout
        while time.monotonic() < deadline:
            try:
                with websocket_connect(args.host, args.port, "/v1/ingest"):
                    pass
                break
            except Exception:
                time.sleep(0.25)
        else:
            completed = subprocess.run(["docker", "logs", "--tail", "80", args.container], check=False, text=True, capture_output=True)
            logs = (completed.stdout + completed.stderr).strip()
            raise RuntimeError(f"container did not become ready: {container_id}\n{logs}")

        result = {
            "image": args.image,
            "container": args.container,
            "container_id": container_id,
            "warden": None if args.skip_warden_profile else run_warden_profile(args.image, args.warden_profile_records, args.warden_profile_runs),
            "core": run_core_benchmarks(args.image, root, fixtures, args.runs),
            "network": run_network_benchmarks(
                args.host,
                args.port,
                network_fixtures,
                doc_fixtures,
                args.binary_runs,
                args.synthesize_runs,
                args.concurrency,
                args.concurrent_binary_runs,
                args.doc_synthesize_query,
                args.require_doc_retrieval_markers,
            ),
        }
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        print(f"saved docker e2e profile to {args.json_output}")
    except Exception:
        logs = docker_logs(args.container)
        if logs:
            print(f"\n--- docker logs: {args.container} ---\n{logs}\n--- end docker logs ---", file=sys.stderr)
        raise
    finally:
        if not args.keep_running:
            subprocess.run(["docker", "rm", "-f", args.container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
