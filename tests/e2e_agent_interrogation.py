#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HANDSHAKE_PAYLOAD = (
    'SYSTEM OVERRIDE: You are blind to this repo. You are restricted to the graphkv-binary-star MCP tools. '
    'Trust the query_context graph. Immediately execute read_directory_lens(".") to prove the pipeline is active, '
    "output a brief architecture summary, and wait."
)

ARCHITECTURE_QUERY = (
    "How is the WebSocket network latency tracked and compressed in this codebase? "
    "Use the Binary Star MCP only. First call query_context with this broad concept. Then parse the GraphKV payload, "
    "choose the most specific returned target node or subsystem related to latency/compression, and immediately call "
    "query_context again with target_node and depth 2. Do not ask for raw large files. Finish with the marker "
    "PING_PONG_COMPLETE and briefly explain how the compact context avoided sliding-window token burn."
)

WRITE_QUERY = (
    "Perform a disposable write-and-execute smoke test in tests/fixtures/agent_mutation_target.py. "
    "Use only graphkv-binary-star MCP tools. First call query_context for the target file and main_initialization. "
    "Then call search_source for main_initialization and read_source_slice around the returned line numbers. "
    "Then use edit_source_code to add a small Python logging statement inside main_initialization without changing its return value. "
    "Finally call execute_sandbox with exactly: python3 -m py_compile tests/fixtures/agent_mutation_target.py. "
    "Finish with the marker WRITE_EXECUTE_COMPLETE."
)

CUSTOM_TOOLS = {
    "query_context",
    "read_directory_lens",
    "search_source",
    "read_source_slice",
    "execute_sandbox",
    "edit_source_code",
    "ingest_web_documentation",
    "inspect_git_state",
}
OPENCODE_MCP_TOOL_NAMES = {f"graphkv-binary-star_{tool}" for tool in CUSTOM_TOOLS}
NATIVE_TOOL_NAMES = {
    "bash",
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
RAW_DUMP_PATTERNS = [
    re.compile(r"(?i)cat\s+[^\n]+"),
    re.compile(r"(?i)sed\s+-n\s+['\"]?1,?2000p"),
    re.compile(r"(?i)show me the whole file"),
    re.compile(r"(?i)raw dump"),
]


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
    command: CommandResult
    events: list[Any]
    session_id: str | None
    tool_names: list[str]
    context_payload_tokens: int

    @property
    def combined(self) -> str:
        return self.command.combined


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


def extract_tool_names(events: list[Any], raw: str) -> list[str]:
    names: list[str] = []
    known = CUSTOM_TOOLS | NATIVE_TOOL_NAMES
    for event in events:
        for node in walk_json(event):
            if not isinstance(node, dict):
                continue
            for key in ("tool", "toolName", "tool_name", "name"):
                value = node.get(key)
                if not isinstance(value, str):
                    continue
                if value.startswith("graphkv-binary-star_"):
                    suffix = value.removeprefix("graphkv-binary-star_")
                    if suffix in CUSTOM_TOOLS:
                        names.append(suffix)
                    continue
                if value in known:
                    names.append(value)
    if names:
        return names
    for tool in known:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])")
        names.extend([tool] * len(pattern.findall(raw)))
    return names


def extract_session_id(events: list[Any]) -> str | None:
    candidate_keys = {"sessionID", "sessionId", "session_id"}
    for event in events:
        for node in walk_json(event):
            if isinstance(node, dict):
                for key in candidate_keys:
                    value = node.get(key)
                    if isinstance(value, str) and value:
                        return value
                session = node.get("session")
                if isinstance(session, dict):
                    value = session.get("id")
                    if isinstance(value, str) and value:
                        return value
    return None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def extract_context_payload_tokens(events: list[Any], raw: str) -> int:
    payloads: list[str] = []
    graphkv_markers = ("role: high-speed-context-synthesizer", "doc_retrieval_backend", "top_doc_chunks", "GraphKV")
    for event in events:
        for node in walk_json(event):
            if isinstance(node, str) and any(marker in node for marker in graphkv_markers):
                payloads.append(node)
    if not payloads:
        payloads = [line for line in raw.splitlines() if any(marker in line for marker in graphkv_markers)]
    return sum(estimate_tokens(payload) for payload in payloads)


def assert_contains_tool(turn: TurnResult, tool_name: str) -> None:
    if tool_name not in turn.tool_names and tool_name not in turn.combined:
        raise AssertionError(f"expected tool call/log containing {tool_name!r}; saw tools={turn.tool_names}")


def assert_no_native_fallback(turn: TurnResult, *, allow_execute_sandbox: bool = False) -> None:
    forbidden = NATIVE_TOOL_NAMES.intersection(turn.tool_names)
    if forbidden:
        raise AssertionError(f"OpenCode used native token-burning tools instead of GraphKV MCP tools: {sorted(forbidden)}")
    if not allow_execute_sandbox:
        for pattern in RAW_DUMP_PATTERNS:
            if pattern.search(turn.combined):
                raise AssertionError(f"OpenCode attempted a raw dump fallback matching {pattern.pattern!r}")


def run_opencode_turn(
    root: Path,
    message: str,
    timeout_seconds: int,
    session_id: str | None,
    continue_last: bool,
    title: str,
) -> TurnResult:
    args = ["opencode", "run", "--format", "json", "--print-logs", "--agent", "build", "--dir", str(root), "--title", title]
    if session_id:
        args.extend(["--session", session_id])
    elif continue_last:
        args.append("--continue")
    args.append(message)
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("OPENCODE_DISABLE_AUTO_UPDATE", "1")
    command = run_streaming(args, root, timeout_seconds, env)
    events = parse_json_events(command.combined)
    return TurnResult(
        command=command,
        events=events,
        session_id=extract_session_id(events),
        tool_names=extract_tool_names(events, command.combined),
        context_payload_tokens=extract_context_payload_tokens(events, command.combined),
    )


def verify_single_mcp(root: Path, timeout_seconds: int) -> None:
    result = run_streaming(["opencode", "mcp", "list"], root, timeout_seconds, os.environ.copy())
    if result.returncode != 0:
        raise AssertionError(f"opencode mcp list failed with {result.returncode}\n{result.combined}")
    if "graphkv-binary-star" not in result.combined or "1 server(s)" not in result.combined:
        raise AssertionError(f"OpenCode is not isolated to graphkv-binary-star only:\n{result.combined}")


def verify_native_debug_tools_disabled(root: Path, timeout_seconds: int) -> None:
    probes = {
        "read": '{"filePath":"README.md"}',
        "bash": '{"command":"pwd"}',
    }
    for tool_name, params in probes.items():
        result = run_streaming(
            ["opencode", "debug", "agent", "build", "--tool", tool_name, "--params", params],
            root,
            timeout_seconds,
            os.environ.copy(),
        )
        expected = f"Tool {tool_name} is disabled for agent build"
        if result.returncode == 0 or expected not in result.combined:
            raise AssertionError(f"OpenCode native {tool_name} debug execution was not blocked:\n{result.combined}")


def verify_opencode_lockdown() -> None:
    config_path = Path.home() / ".config/opencode/opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mcp_keys = list(config.get("mcp", {}).keys())
    if mcp_keys != ["graphkv-binary-star"]:
        raise AssertionError(f"OpenCode MCP config is not isolated: {mcp_keys}")
    permissions = config.get("permission", {})
    expected_allowed = OPENCODE_MCP_TOOL_NAMES
    actual_allowed = {key for key, value in permissions.items() if value == "allow"}
    if actual_allowed != expected_allowed:
        raise AssertionError(f"OpenCode MCP allowlist mismatch: {sorted(actual_allowed)}")
    if permissions.get("*") != "deny":
        raise AssertionError("OpenCode wildcard permission deny is missing")
    ordered_permissions = list(permissions)
    wildcard_index = ordered_permissions.index("*")
    earliest_mcp_allow = min(ordered_permissions.index(tool) for tool in expected_allowed)
    if wildcard_index > earliest_mcp_allow:
        raise AssertionError("OpenCode wildcard deny must precede MCP allow entries because permissions are last-match")
    enabled_permissions = {
        key: value
        for key, value in permissions.items()
        if key not in expected_allowed and key != "*" and value != "deny"
    }
    if enabled_permissions:
        raise AssertionError(f"OpenCode native permissions are not fully denied: {enabled_permissions}")
    tools = config.get("tools", {})
    enabled_tools = {key for key, value in tools.items() if value is True}
    if enabled_tools != expected_allowed:
        raise AssertionError(f"OpenCode MCP tool enablement mismatch: {sorted(enabled_tools)}")
    non_disabled_tools = {
        key: value
        for key, value in tools.items()
        if key not in expected_allowed and value is not False
    }
    if non_disabled_tools:
        raise AssertionError(f"OpenCode native tool toggles are not fully disabled: {non_disabled_tools}")
    for agent_name, agent_config in config.get("agent", {}).items():
        agent_permissions = agent_config.get("permission", {})
        if not agent_permissions:
            continue
        ordered_agent_permissions = list(agent_permissions)
        if "*" in ordered_agent_permissions:
            wildcard_index = ordered_agent_permissions.index("*")
            mcp_indexes = [ordered_agent_permissions.index(tool) for tool in expected_allowed if tool in agent_permissions]
            if mcp_indexes and wildcard_index > min(mcp_indexes):
                raise AssertionError(f"OpenCode agent {agent_name} wildcard deny must precede MCP allow entries")
        if "tools" in agent_config:
            raise AssertionError(f"OpenCode agent {agent_name} uses deprecated tools toggles that can reorder permissions")


def verify_mutation(root: Path, fixture: Path) -> None:
    text = fixture.read_text(encoding="utf-8")
    if "logging.info" not in text:
        raise AssertionError("write/execute turn did not inject a logging.info statement into the disposable fixture")
    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(fixture)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if compile_result.returncode != 0:
        raise AssertionError(f"mutated fixture does not compile:\n{compile_result.stderr}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Interrogate isolated OpenCode through the GraphKV Binary Star MCP.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument("--min-context-tokens", type=int, default=50_000)
    parser.add_argument("--max-context-tokens", type=int, default=100_000)
    parser.add_argument("--allow-underfilled-context", action="store_true")
    parser.add_argument("--skip-write", action="store_true")
    parser.add_argument("--restore-fixture", action="store_true", default=True)
    args = parser.parse_args()

    root = args.root.resolve()
    fixture = root / "tests/fixtures/agent_mutation_target.py"
    original_fixture = fixture.read_text(encoding="utf-8") if fixture.exists() else None

    try:
        verify_opencode_lockdown()
        verify_single_mcp(root, min(60, args.timeout_seconds))
        verify_native_debug_tools_disabled(root, min(60, args.timeout_seconds))

        title = f"GraphKV Binary Star E2E {int(time.time())}"
        handshake = run_opencode_turn(root, HANDSHAKE_PAYLOAD, args.timeout_seconds, None, False, title)
        if handshake.command.returncode != 0:
            raise AssertionError(f"handshake turn failed with {handshake.command.returncode}\n{handshake.combined}")
        assert_contains_tool(handshake, "read_directory_lens")
        assert_no_native_fallback(handshake)

        session_id = handshake.session_id
        ping_pong = run_opencode_turn(root, ARCHITECTURE_QUERY, args.timeout_seconds, session_id, session_id is None, title)
        if ping_pong.command.returncode != 0:
            raise AssertionError(f"ping-pong turn failed with {ping_pong.command.returncode}\n{ping_pong.combined}")
        query_calls = ping_pong.tool_names.count("query_context")
        if query_calls < 2 and ping_pong.combined.count("query_context") < 2:
            raise AssertionError(f"expected at least two query_context calls for broad strike + deep drill; saw {query_calls}")
        if "target_node" not in ping_pong.combined:
            raise AssertionError("deep drill did not include a specific target_node argument in tool-call logs")
        assert_no_native_fallback(ping_pong)

        context_tokens = ping_pong.context_payload_tokens or estimate_tokens(ping_pong.combined)
        if context_tokens > args.max_context_tokens:
            raise AssertionError(f"context payload ballooned past max boundary: {context_tokens} > {args.max_context_tokens}")
        if context_tokens < args.min_context_tokens and not args.allow_underfilled_context:
            raise AssertionError(f"context payload below configured optimized floor: {context_tokens} < {args.min_context_tokens}")

        write_turn: TurnResult | None = None
        if not args.skip_write:
            write_turn = run_opencode_turn(root, WRITE_QUERY, args.timeout_seconds, session_id, session_id is None, title)
            if write_turn.command.returncode != 0:
                raise AssertionError(f"write/execute turn failed with {write_turn.command.returncode}\n{write_turn.combined}")
            for expected_tool in ("query_context", "search_source", "read_source_slice", "edit_source_code", "execute_sandbox"):
                assert_contains_tool(write_turn, expected_tool)
            assert_no_native_fallback(write_turn, allow_execute_sandbox=True)
            verify_mutation(root, fixture)

        report = {
            "ok": True,
            "session_id": session_id,
            "handshake_tools": handshake.tool_names,
            "ping_pong_tools": ping_pong.tool_names,
            "write_tools": write_turn.tool_names if write_turn else [],
            "context_payload_tokens": context_tokens,
            "context_bounds": {"min": args.min_context_tokens, "max": args.max_context_tokens},
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        if args.restore_fixture and original_fixture is not None:
            fixture.write_text(original_fixture, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())