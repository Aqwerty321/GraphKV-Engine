#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_fixtures(root: Path) -> list[Path]:
    return [
        root / "benchmarks/data/python_send_scip_ws.scip",
        root / "benchmarks/data/python_repo.scip",
    ]


def run_command(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def cpu_model() -> str:
    try:
        return run_command(["lscpu"], cwd=None).split("Model name:", 1)[1].splitlines()[0].strip()
    except Exception:
        return platform.processor() or "unknown"


def compiler_version() -> str:
    try:
        return run_command(["clang++", "--version"]).splitlines()[0].strip()
    except Exception:
        return "unknown"


def parse_benchmark_output(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed


def benchmark_fixture(benchmark_path: Path, fixture: Path, runs: int) -> dict[str, object]:
    run_rows: list[dict[str, float | int]] = []
    for run_index in range(1, runs + 1):
        output = run_command([str(benchmark_path), str(fixture)])
        parsed = parse_benchmark_output(output)
        run_rows.append(
            {
                "run": run_index,
                "bytes": int(parsed["bytes"]),
                "iterations": int(parsed["iterations"]),
                "mean_us": float(parsed["mean_us"]),
                "median_us": float(parsed["median_us"]),
                "p99_us": float(parsed["p99_us"]),
                "checksum": int(parsed["checksum"]),
            }
        )

    mean_values = [float(row["mean_us"]) for row in run_rows]
    median_values = [float(row["median_us"]) for row in run_rows]
    p99_values = [float(row["p99_us"]) for row in run_rows]
    return {
        "fixture": str(fixture),
        "bytes": int(run_rows[0]["bytes"]),
        "iterations": int(run_rows[0]["iterations"]),
        "runs": run_rows,
        "summary": {
            "mean_of_means_us": statistics.fmean(mean_values),
            "stdev_mean_us": statistics.stdev(mean_values) if len(mean_values) > 1 else 0.0,
            "best_mean_us": min(mean_values),
            "worst_mean_us": max(mean_values),
            "mean_of_medians_us": statistics.fmean(median_values),
            "mean_of_p99_us": statistics.fmean(p99_values),
            "worst_p99_us": max(p99_values),
        },
    }


def markdown_report(metadata: dict[str, object], fixture_results: list[dict[str, object]]) -> str:
    lines = [
        "# Benchmark Run Report",
        "",
        f"Generated: {metadata['generated_at']}",
        "",
        "## Host",
        "",
        f"- CPU: {metadata['cpu']}",
        f"- Logical threads: {metadata['logical_threads']}",
        f"- Kernel: {metadata['kernel']}",
        f"- Compiler: {metadata['compiler']}",
        f"- Benchmark binary: {metadata['benchmark_binary']}",
        f"- Runs per fixture: {metadata['runs_per_fixture']}",
        "",
        "## Fixtures",
        "",
        "| Fixture | Bytes | Mean Of Means (us) | Mean Stddev (us) | Best Mean (us) | Worst Mean (us) | Mean Of P99 (us) | Worst P99 (us) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for result in fixture_results:
        summary = result["summary"]
        lines.append(
            "| {fixture} | {bytes:,} | {mean:.2f} | {stdev:.2f} | {best:.2f} | {worst:.2f} | {mean_p99:.2f} | {worst_p99:.2f} |".format(
                fixture=result["fixture"],
                bytes=result["bytes"],
                mean=summary["mean_of_means_us"],
                stdev=summary["stdev_mean_us"],
                best=summary["best_mean_us"],
                worst=summary["worst_mean_us"],
                mean_p99=summary["mean_of_p99_us"],
                worst_p99=summary["worst_p99_us"],
            )
        )

    for result in fixture_results:
        lines.extend(
            [
                "",
                f"## {result['fixture']}",
                "",
                "| Run | Mean Latency (us) | Median Latency (us) | P99 Latency (us) |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for row in result["runs"]:
            lines.append(
                "| {run} | {mean:.2f} | {median:.2f} | {p99:.2f} |".format(
                    run=row["run"],
                    mean=row["mean_us"],
                    median=row["median_us"],
                    p99=row["p99_us"],
                )
            )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Timing still happens inside the C++ benchmark binary with `std::chrono::high_resolution_clock`.",
            "- This Python script only refreshes corpora, launches repeated runs, and writes the saved report.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description="Run GraphKV ingest benchmarks and save the results.")
    parser.add_argument("--build-dir", type=Path, default=root / "build-benchmark")
    parser.add_argument("--benchmark", type=Path, default=None)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--skip-refresh-real-corpora", action="store_true")
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--markdown-output", type=Path, default=root / "benchmarks/results/latest.md")
    parser.add_argument("--json-output", type=Path, default=root / "benchmarks/results/latest.json")
    args = parser.parse_args()

    if args.runs <= 0:
        raise SystemExit("--runs must be greater than zero")

    benchmark_path = args.benchmark or (args.build_dir / "ingest_latency_benchmark")
    benchmark_path = benchmark_path.resolve()
    fixtures = [path.resolve() for path in args.fixture] or default_fixtures(root)

    if not args.skip_refresh_real_corpora:
        subprocess.run([str(root / "benchmarks/generate_real_corpora.sh")], cwd=str(root), check=True)

    if not benchmark_path.exists():
        raise SystemExit(f"benchmark binary not found: {benchmark_path}")

    results_dir = args.markdown_output.resolve().parent
    results_dir.mkdir(parents=True, exist_ok=True)
    args.json_output.resolve().parent.mkdir(parents=True, exist_ok=True)

    fixture_results = [benchmark_fixture(benchmark_path, fixture, args.runs) for fixture in fixtures]
    metadata = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "cpu": cpu_model(),
        "logical_threads": os.cpu_count() or 0,
        "kernel": f"{platform.system()} {platform.release()}",
        "compiler": compiler_version(),
        "benchmark_binary": str(benchmark_path),
        "runs_per_fixture": args.runs,
    }

    markdown = markdown_report(metadata, fixture_results)
    args.markdown_output.resolve().write_text(markdown)
    args.json_output.resolve().write_text(
        json.dumps({"metadata": metadata, "fixtures": fixture_results}, indent=2) + "\n"
    )

    print(f"saved markdown report to {args.markdown_output.resolve()}")
    print(f"saved json report to {args.json_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())