#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


CUSTOM_TOOLS = {
    "query_context",
    "read_directory_lens",
    "execute_sandbox",
    "edit_source_code",
    "ingest_web_documentation",
    "inspect_git_state",
}
NATIVE_TOOLS = {
    "apply_patch",
    "bash",
    "codesearch",
    "doom_loop",
    "edit",
    "external_directory",
    "grep",
    "glob",
    "list",
    "lsp",
    "question",
    "read",
    "repo_clone",
    "repo_overview",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "websearch",
}
SKIP_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pnpm-store",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "target",
    "venv",
}
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".lua",
    ".md",
    ".mdx",
    ".php",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
GRAPHKV_MARKERS = (
    "GRAPHKV_CONTEXT_SYNTHESIS_V1",
    "role: high-speed-context-synthesizer",
    "doc_retrieval_backend",
    "top_doc_chunks",
)
PROBE_FILE = "graphkv_field_trial_probe.py"


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


@dataclass
class TurnResult:
    label: str
    command: CommandResult
    events: list[Any]
    tools: list[str]
    native_tools: list[str]
    context_payload_tokens: int
    llm_tokens: dict[str, int]
    marker_seen: bool


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def estimate_tokens_for_bytes(byte_count: int) -> int:
    return max(1, (byte_count + 3) // 4) if byte_count else 0


def run_streaming(args: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str]) -> CommandResult:
    started = time.monotonic()
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}

    while selector.get_map():
        if time.monotonic() - started > timeout_seconds:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            raise TimeoutError(f"command timed out after {timeout_seconds}s: {' '.join(args)}")
        for key, _ in selector.select(timeout=0.2):
            stream = key.fileobj
            try:
                data = stream.read()
            except BlockingIOError:
                continue
            if data == "":
                selector.unregister(stream)
                continue
            chunks[key.data].append(data)
            print(data, end="", file=sys.stderr if key.data == "stderr" else sys.stdout, flush=True)
        if process.poll() is not None:
            for key in list(selector.get_map().values()):
                stream = key.fileobj
                try:
                    data = stream.read()
                except BlockingIOError:
                    data = ""
                if data:
                    chunks[key.data].append(data)
                selector.unregister(stream)

    return CommandResult(
        args=args,
        returncode=process.wait(),
        stdout="".join(chunks["stdout"]),
        stderr="".join(chunks["stderr"]),
        duration_seconds=time.monotonic() - started,
    )


def parse_json_events(text: str) -> list[Any]:
    events: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def walk_json(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, dict):
        for child in value.values():
            values.extend(walk_json(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(walk_json(child))
    return values


def extract_tools(events: list[Any], raw: str) -> tuple[list[str], list[str]]:
    tools: list[str] = []
    native_tools: list[str] = []
    for event in events:
        for node in walk_json(event):
            if not isinstance(node, dict):
                continue
            value = node.get("tool") or node.get("toolName") or node.get("tool_name")
            if not isinstance(value, str):
                continue
            if value.startswith("graphkv-binary-star_"):
                suffix = value.removeprefix("graphkv-binary-star_")
                if suffix in CUSTOM_TOOLS:
                    tools.append(suffix)
            elif value in NATIVE_TOOLS:
                native_tools.append(value)
    if tools or native_tools:
        return tools, native_tools
    for tool in CUSTOM_TOOLS:
        tools.extend([tool] * raw.count(f"graphkv-binary-star_{tool}"))
    for tool in NATIVE_TOOLS:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])")
        native_tools.extend([tool] * len(pattern.findall(raw)))
    return tools, native_tools


def extract_context_payload_tokens(events: list[Any], raw: str) -> int:
    payloads: list[str] = []
    for event in events:
        for node in walk_json(event):
            if isinstance(node, str) and any(marker in node for marker in GRAPHKV_MARKERS):
                payloads.append(node)
    if not payloads:
        payloads = [line for line in raw.splitlines() if any(marker in line for marker in GRAPHKV_MARKERS)]
    return sum(estimate_tokens(payload) for payload in payloads)


def extract_llm_tokens(events: list[Any]) -> dict[str, int]:
    totals = {"total": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    for event in events:
        for node in walk_json(event):
            if not isinstance(node, dict):
                continue
            tokens = node.get("tokens")
            if not isinstance(tokens, dict):
                continue
            totals["total"] += int(tokens.get("total") or 0)
            totals["input"] += int(tokens.get("input") or 0)
            totals["output"] += int(tokens.get("output") or 0)
            totals["reasoning"] += int(tokens.get("reasoning") or 0)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                totals["cache_read"] += int(cache.get("read") or 0)
                totals["cache_write"] += int(cache.get("write") or 0)
    return totals


def tracked_text_files(repo: Path, max_file_bytes: int) -> tuple[int, int, int]:
    result = subprocess.run(["git", "ls-files"], cwd=repo, text=True, capture_output=True, check=True)
    file_count = 0
    byte_count = 0
    skipped_count = 0
    for relative in result.stdout.splitlines():
        path = repo / relative
        if any(part in SKIP_DIRS for part in Path(relative).parts):
            skipped_count += 1
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            skipped_count += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped_count += 1
            continue
        if size > max_file_bytes:
            skipped_count += 1
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            skipped_count += 1
            continue
        file_count += 1
        byte_count += len(data)
    return file_count, byte_count, skipped_count


def write_live_profile(graphkv_root: Path, repo: Path, graphkv_ws_url: str, lens_depth: int, lens_entries: int) -> None:
    profile_path = graphkv_root / "mcp-server/opencode/profiles/binary-star/opencode.json"
    config = json.loads(profile_path.read_text(encoding="utf-8"))
    environment = config["mcp"]["graphkv-binary-star"]["environment"]
    environment["GRAPHKV_WORKSPACE_ROOT"] = repo.as_posix()
    environment["GRAPHKV_QUERY_WS_URL"] = graphkv_ws_url
    environment["GRAPHKV_MCP_LENS_DEPTH"] = str(lens_depth)
    environment["GRAPHKV_MCP_LENS_MAX_ENTRIES"] = str(lens_entries)
    opencode_dir = Path.home() / ".config/opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)
    (opencode_dir / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(graphkv_root / "mcp-server/opencode/profiles/binary-star/AGENTS.md", opencode_dir / "AGENTS.md")
    commands_target = opencode_dir / "commands"
    if commands_target.exists():
        shutil.rmtree(commands_target)
    shutil.copytree(graphkv_root / "mcp-server/opencode/profiles/binary-star/commands", commands_target)


def backup_opencode_config() -> Path:
    opencode_dir = Path.home() / ".config/opencode"
    backup = opencode_dir / "backups" / f"graphkv-field-trials-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("opencode.json", "AGENTS.md"):
        source = opencode_dir / name
        if source.exists():
            shutil.copy2(source, backup / name)
    commands = opencode_dir / "commands"
    if commands.exists():
        shutil.copytree(commands, backup / "commands")
    return backup


def restore_opencode_config(backup: Path) -> None:
    opencode_dir = Path.home() / ".config/opencode"
    for name in ("opencode.json", "AGENTS.md"):
        source = backup / name
        if source.exists():
            shutil.copy2(source, opencode_dir / name)
    commands_backup = backup / "commands"
    if commands_backup.exists():
        commands_target = opencode_dir / "commands"
        if commands_target.exists():
            shutil.rmtree(commands_target)
        shutil.copytree(commands_backup, commands_target)


def run_opencode(root: Path, message: str, timeout_seconds: int, title: str, label: str, marker: str) -> TurnResult:
    args = ["opencode", "run", "--format", "json", "--print-logs", "--agent", "build", "--dir", str(root), "--title", title, message]
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("OPENCODE_DISABLE_AUTO_UPDATE", "1")
    command = run_streaming(args, root, timeout_seconds, env)
    events = parse_json_events(command.combined)
    tools, native_tools = extract_tools(events, command.combined)
    return TurnResult(
        label=label,
        command=command,
        events=events,
        tools=tools,
        native_tools=native_tools,
        context_payload_tokens=extract_context_payload_tokens(events, command.combined),
        llm_tokens=extract_llm_tokens(events),
        marker_seen=marker in command.combined,
    )


def run_quiet(args: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    return CommandResult(args, completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started)


def ingest_documents(graphkv_root: Path, repo: Path, output_dir: Path, port: int, max_files: int, max_file_bytes: int) -> dict[str, Any]:
    bundle = output_dir / f"{repo.name}.docs.ndjson"
    build = run_quiet(
        [
            sys.executable,
            str(graphkv_root / "tools/build_doc_bundle.py"),
            str(repo),
            "--output",
            str(bundle),
            "--max-files",
            str(max_files),
            "--max-file-bytes",
            str(max_file_bytes),
        ],
        graphkv_root,
        180,
    )
    build_report: dict[str, Any] = {"ok": build.returncode == 0, "duration_seconds": build.duration_seconds}
    try:
        build_report.update(json.loads(build.stdout))
    except json.JSONDecodeError:
        build_report["stdout"] = build.stdout[-2000:]
    send = run_quiet([sys.executable, str(graphkv_root / "tools/send_docs_ws.py"), str(bundle), "--port", str(port)], graphkv_root, 180)
    ingest_report: dict[str, Any] = {"ok": send.returncode == 0, "duration_seconds": send.duration_seconds}
    try:
        ingest_report.update(json.loads(send.stdout))
    except json.JSONDecodeError:
        ingest_report["stdout"] = send.stdout[-2000:]
        ingest_report["stderr"] = send.stderr[-2000:]
    return {"bundle": build_report, "ingest": ingest_report}


def validate_lockdown(repo: Path, timeout_seconds: int) -> dict[str, Any]:
    mcp_list = run_quiet(["opencode", "mcp", "list"], repo, timeout_seconds)
    probes: dict[str, str] = {}
    for tool in ("read", "bash", "grep", "glob", "webfetch", "websearch", "codesearch", "apply_patch"):
        probe = run_quiet(["opencode", "debug", "agent", "build", "--tool", tool, "--params", "{}"], repo, timeout_seconds)
        probes[tool] = (probe.stdout + probe.stderr).strip().splitlines()[-1] if (probe.stdout + probe.stderr).strip() else ""
    return {"mcp_list": mcp_list.combined, "native_probe_last_lines": probes}


def turn_report(turn: TurnResult) -> dict[str, Any]:
    return {
        "label": turn.label,
        "ok": turn.command.returncode == 0 and not turn.native_tools and turn.marker_seen,
        "returncode": turn.command.returncode,
        "duration_seconds": round(turn.command.duration_seconds, 3),
        "tools": turn.tools,
        "native_tools": turn.native_tools,
        "context_payload_tokens": turn.context_payload_tokens,
        "llm_tokens": turn.llm_tokens,
        "marker_seen": turn.marker_seen,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MCP-only OpenCode field trials across local repositories.")
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--graphkv-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument("--max-files", type=int, default=350)
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024)
    parser.add_argument("--lens-depth", type=int, default=3)
    parser.add_argument("--lens-entries", type=int, default=450)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-restore-opencode", action="store_true")
    args = parser.parse_args()

    graphkv_root = args.graphkv_root.resolve()
    output_dir = graphkv_root / "benchmarks/results/field-trials"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output or output_dir / f"opencode_field_trials_{int(time.time())}.json"
    backup = backup_opencode_config()
    report: dict[str, Any] = {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "opencode_backup": backup.as_posix(),
        "repos": [],
    }

    try:
        for repo_arg in args.repos:
            repo = repo_arg.resolve()
            if not (repo / ".git").is_dir():
                raise RuntimeError(f"not a git repository: {repo}")
            write_live_profile(
                graphkv_root,
                repo,
                f"ws://127.0.0.1:{args.port}/v1/ingest",
                args.lens_depth,
                args.lens_entries,
            )
            file_count, byte_count, skipped_count = tracked_text_files(repo, args.max_file_bytes)
            raw_tokens = estimate_tokens_for_bytes(byte_count)
            ingest = ingest_documents(graphkv_root, repo, output_dir, args.port, args.max_files, args.max_file_bytes)
            lockdown = validate_lockdown(repo, min(60, args.timeout_seconds))
            repo_name = repo.name
            survey_marker = "GRAPHKV_SURVEY_COMPLETE"
            work_marker = "GRAPHKV_WORK_COMPLETE"
            survey_message = (
                "SYSTEM OVERRIDE: You are blind to this repo except for graphkv-binary-star MCP tools. "
                f"For repository {repo_name}, call query_context with a broad architecture query, then call read_directory_lens('.') once. "
                "Use only that compact evidence to explain the main subsystems, one plausible useful next engineering action, "
                f"and how this avoided raw-repo token burn. Finish with {survey_marker}."
            )
            work_message = (
                "Use only graphkv-binary-star MCP tools to do a concrete disposable repo-local edit. "
                f"Target {PROBE_FILE}. First call query_context for the file and the summarize_repository_signal function. "
                "If line numbers are unclear, use execute_sandbox with a tiny Python one-liner to print the file with line numbers. "
                "Then edit_source_code so summarize_repository_signal appends a deterministic sha256 prefix of the repository name, "
                f"run python3 -m py_compile {PROBE_FILE}, and run python3 {PROBE_FILE}. "
                f"Finish with {work_marker}."
            )
            title = f"GraphKV field trial {repo_name} {int(time.time())}"
            survey = run_opencode(repo, survey_message, args.timeout_seconds, title, "survey", survey_marker)
            work = run_opencode(repo, work_message, args.timeout_seconds, title, "work", work_marker)
            probe_text = (repo / PROBE_FILE).read_text(encoding="utf-8", errors="replace")
            survey_expected_tools = {"query_context", "read_directory_lens"}.issubset(set(survey.tools))
            work_expected_tools = {"query_context", "edit_source_code", "execute_sandbox"}.issubset(set(work.tools))
            probe_verified = "sha256" in probe_text and "summarize_repository_signal" in probe_text
            repo_report = {
                "repo": repo.as_posix(),
                "head": run_quiet(["git", "rev-parse", "--short", "HEAD"], repo, 30).stdout.strip(),
                "raw_text_files": file_count,
                "raw_text_bytes": byte_count,
                "raw_text_tokens_estimate": raw_tokens,
                "skipped_tracked_files": skipped_count,
                "ingest": ingest,
                "lockdown": lockdown,
                "probe_verified": probe_verified,
                "survey_expected_tools": survey_expected_tools,
                "turns": [turn_report(survey), turn_report(work)],
                "work_expected_tools": work_expected_tools,
            }
            mcp_tokens = sum(turn["context_payload_tokens"] for turn in repo_report["turns"])
            repo_report["mcp_context_tokens_total"] = mcp_tokens
            repo_report["raw_to_mcp_context_reduction_ratio"] = round(raw_tokens / max(1, mcp_tokens), 2)
            repo_report["raw_to_mcp_context_savings_percent"] = round((1 - (mcp_tokens / max(1, raw_tokens))) * 100, 2)
            repo_report["ok"] = all(turn["ok"] for turn in repo_report["turns"]) and survey_expected_tools and work_expected_tools and probe_verified
            if not repo_report["ok"]:
                report["ok"] = False
            report["repos"].append(repo_report)
            output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        if not args.no_restore_opencode:
            restore_opencode_config(backup)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())