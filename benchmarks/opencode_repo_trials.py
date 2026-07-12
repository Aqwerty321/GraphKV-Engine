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
from textwrap import dedent
from typing import Any


CUSTOM_TOOLS = {
    "query_context",
    "rank_intent_targets",
    "read_directory_lens",
    "search_source",
    "read_source_slice",
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
FAILURE_PROBE_FILE = "graphkv_field_trial_failure_probe.py"
COORDINATE_TOOLS = {"search_source", "read_source_slice"}
PROOF_MARKERS = ("evidence_id", "range_sha256", "before_range_sha256", "edit_proof_id")
EXECUTION_EVIDENCE_MARKERS = ("execution_id", "snapshot_id", "evidence", "failure_memory")
DEFAULT_SCENARIOS = ("survey", "proof_edit", "failure_recovery")


@dataclass(frozen=True)
class RepoSpec:
    name: str
    url: str
    description: str


COMPLEX_REPO_SUITE = (
    RepoSpec("fastapi", "https://github.com/fastapi/fastapi.git", "Python API framework with docs, tests, routing, validation, and async patterns"),
    RepoSpec("httpx", "https://github.com/encode/httpx.git", "Python async HTTP client with transports, auth, CLI, and protocol boundaries"),
    RepoSpec("pydantic", "https://github.com/pydantic/pydantic.git", "Python/Rust validation library with generated schemas and mixed-language build surface"),
    RepoSpec("ripgrep", "https://github.com/BurntSushi/ripgrep.git", "Rust CLI search engine with workspace crates, tests, and platform-specific paths"),
    RepoSpec("redis", "https://github.com/redis/redis.git", "C datastore with networking, persistence, modules, and command implementation layers"),
)


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


def proof_probe_content() -> str:
    return dedent(
        '''\
        #!/usr/bin/env python3
        from __future__ import annotations

        import hashlib
        from pathlib import Path


        def normalize_repository_name(repository_name: str) -> str:
            return repository_name.strip().lower().replace("_", "-").replace(" ", "-")


        def summarize_repository_signal(repository_name: str) -> str:
            normalized = normalize_repository_name(repository_name)
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
            return f"repo={normalized};sha256={digest}"


        if __name__ == "__main__":
            print(summarize_repository_signal(Path.cwd().name))
        '''
    )


def failure_probe_content() -> str:
    return dedent(
        '''\
        #!/usr/bin/env python3
        from __future__ import annotations

        from pathlib import Path


        def normalize_failure_label(repository_name: str | None) -> str:
            return repository_name.strip().lower().replace("_", "-").replace(" ", "-")


        def summarize_failure_signal(repository_name: str | None) -> str:
            label = normalize_failure_label(repository_name)
            return f"failure-probe={label};chars={len(label)}"


        if __name__ == "__main__":
            print(summarize_failure_signal(None))
            print(summarize_failure_signal(Path.cwd().name))
        '''
    )


def seed_probe_files(repo: Path) -> dict[str, str | None]:
    snapshots: dict[str, str | None] = {}
    for relative, content in ((PROBE_FILE, proof_probe_content()), (FAILURE_PROBE_FILE, failure_probe_content())):
        path = repo / relative
        snapshots[relative] = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(content, encoding="utf-8")
    return snapshots


def restore_probe_files(repo: Path, snapshots: dict[str, str | None]) -> None:
    for relative, original in snapshots.items():
        path = repo / relative
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def estimate_tokens_for_bytes(byte_count: int) -> int:
    return max(1, (byte_count + 3) // 4) if byte_count else 0


def run_streaming(args: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str], stream_logs: bool = False) -> CommandResult:
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
            if stream_logs:
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


def iter_json_nodes(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def compact_consecutive_tools(tool_names: list[str]) -> list[str]:
    compacted: list[str] = []
    for tool_name in tool_names:
        if not compacted or compacted[-1] != tool_name:
            compacted.append(tool_name)
    return compacted


def raw_tool_mentions(raw: str) -> tuple[list[str], list[str]]:
    custom_mentions: list[tuple[int, str]] = []
    native_mentions: list[tuple[int, str]] = []
    for tool_name in CUSTOM_TOOLS:
        pattern = re.compile(rf"graphkv-binary-star_{re.escape(tool_name)}")
        custom_mentions.extend((match.start(), tool_name) for match in pattern.finditer(raw))
    for tool_name in NATIVE_TOOLS:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])")
        native_mentions.extend((match.start(), tool_name) for match in pattern.finditer(raw))
    tools = [tool_name for _position, tool_name in sorted(custom_mentions)]
    native_tools = [tool_name for _position, tool_name in sorted(native_mentions)]
    return tools, native_tools


def find_hesitation_violation(tool_names: list[str], *, require_direct_edit: bool = False) -> str | None:
    if tool_names.count("rank_intent_targets") > 1:
        return "HESITATION VIOLATION: rank_intent_targets occurred more than once"
    compacted = compact_consecutive_tools(tool_names)
    for position, tool_name in enumerate(compacted):
        if tool_name == "rank_intent_targets" and "read_directory_lens" in compacted[position + 1 :]:
            return "HESITATION VIOLATION: read_directory_lens occurred after intent ranking"
    for position, tool_name in enumerate(compacted):
        if tool_name not in COORDINATE_TOOLS:
            continue
        following = compacted[position + 1] if position + 1 < len(compacted) else None
        if require_direct_edit and following != "edit_source_code":
            return (
                "HESITATION VIOLATION: expected edit_source_code immediately after "
                f"{tool_name}, saw {following or 'end-of-turn'}"
            )
        for later_tool in compacted[position + 1 :]:
            if later_tool == "read_directory_lens":
                return "HESITATION VIOLATION: read_directory_lens occurred after source coordinates"
    return None


def assert_no_hesitation_violation(tool_names: list[str], *, require_direct_edit: bool = False) -> None:
    violation = find_hesitation_violation(tool_names, require_direct_edit=require_direct_edit)
    if violation is not None:
        raise AssertionError(f"{violation}; tools={tool_names}")


def assert_direct_edit_pipeline_tools(tool_names: list[str]) -> None:
    compacted = compact_consecutive_tools(tool_names)
    if "read_source_slice" in compacted:
        raise AssertionError(f"HESITATION VIOLATION: read_source_slice used in direct search edit pipeline; tools={tool_names}")
    if "read_directory_lens" in compacted:
        raise AssertionError(f"HESITATION VIOLATION: read_directory_lens used during edit pipeline; tools={tool_names}")
    required_order = ["query_context", "search_source", "edit_source_code", "execute_sandbox"]
    positions: dict[str, int] = {}
    for tool_name in required_order:
        if tool_name not in compacted:
            raise AssertionError(f"expected deterministic edit pipeline tool {tool_name!r}; saw tools={tool_names}")
        positions[tool_name] = compacted.index(tool_name)
    if not all(positions[left] < positions[right] for left, right in zip(required_order, required_order[1:])):
        raise AssertionError(f"deterministic edit pipeline order was violated; tools={tool_names}")
    if compacted[positions["search_source"] + 1] != "edit_source_code":
        raise AssertionError(f"HESITATION VIOLATION: edit_source_code did not immediately follow search_source; tools={tool_names}")


def direct_edit_pipeline_ok(tool_names: list[str]) -> bool:
    try:
        assert_direct_edit_pipeline_tools(tool_names)
    except AssertionError:
        return False
    return True


def proof_edit_pipeline_ok_tools(tool_names: list[str]) -> bool:
    compacted = compact_consecutive_tools(tool_names)
    required = ["query_context", "edit_source_code", "execute_sandbox"]
    if any(tool not in compacted for tool in required):
        return False
    query_position = compacted.index("query_context")
    edit_position = compacted.index("edit_source_code")
    sandbox_position = compacted.index("execute_sandbox")
    if not query_position < edit_position < sandbox_position:
        return False
    if "read_directory_lens" in compacted[query_position:edit_position]:
        return False
    if "search_source" in compacted:
        search_position = compacted.index("search_source")
        if search_position > edit_position or compacted[search_position + 1] != "edit_source_code":
            return False
    if "read_source_slice" in compacted[:edit_position]:
        return False
    return True


def failure_recovery_pipeline_ok_tools(tool_names: list[str]) -> bool:
    compacted = compact_consecutive_tools(tool_names)
    required = ["execute_sandbox", "rank_intent_targets", "query_context", "edit_source_code"]
    if any(tool not in compacted for tool in required):
        return False
    first_sandbox = compacted.index("execute_sandbox")
    rank_position = compacted.index("rank_intent_targets")
    query_position = compacted.index("query_context")
    edit_position = compacted.index("edit_source_code")
    if not first_sandbox < rank_position < query_position < edit_position:
        return False
    if "read_directory_lens" in compacted[first_sandbox:edit_position]:
        return False
    return "execute_sandbox" in compacted[edit_position + 1 :]


def proof_markers_seen(turn: TurnResult) -> dict[str, bool]:
    combined = turn.command.combined
    return {marker: marker in combined for marker in PROOF_MARKERS}


def execution_markers_seen(turn: TurnResult) -> dict[str, bool]:
    combined = turn.command.combined
    return {marker: marker in combined for marker in EXECUTION_EVIDENCE_MARKERS}


def extract_tools(events: list[Any], raw: str) -> tuple[list[str], list[str]]:
    tools: list[str] = []
    native_tools: list[str] = []
    for event in events:
        for node in iter_json_nodes(event):
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
    return raw_tool_mentions(raw)


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def tool_suffix(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("graphkv-binary-star_"):
        return value.removeprefix("graphkv-binary-star_")
    if value in CUSTOM_TOOLS:
        return value
    return None


def tool_call_records(events: list[Any], tool_name: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for event in events:
        for node in iter_json_nodes(event):
            if not isinstance(node, dict) or id(node) in seen:
                continue
            suffix = tool_suffix(node.get("tool") or node.get("toolName") or node.get("tool_name") or node.get("name"))
            if suffix != tool_name:
                continue
            seen.add(id(node))
            state = node.get("state") if isinstance(node.get("state"), dict) else {}
            input_value = parse_jsonish(state.get("input") or node.get("input") or node.get("arguments") or node.get("args") or {})
            output_value = parse_jsonish(state.get("output") or node.get("output") or node.get("result") or {})
            records.append({"input": input_value, "output": output_value})
    return records


def sandbox_usage(turn: TurnResult) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    for record in tool_call_records(turn.events, "execute_sandbox"):
        input_value = record.get("input") if isinstance(record.get("input"), dict) else {}
        output_value = record.get("output") if isinstance(record.get("output"), dict) else {}
        evidence = output_value.get("evidence") if isinstance(output_value.get("evidence"), dict) else {}
        failure_memory = output_value.get("failure_memory") if isinstance(output_value.get("failure_memory"), dict) else {}
        calls.append(
            {
                "command": input_value.get("command") or output_value.get("command"),
                "execution_tier": output_value.get("execution_tier"),
                "patch_id_present": bool(input_value.get("patch_id") or output_value.get("patch_id")),
                "ok": output_value.get("ok"),
                "exit_code": output_value.get("exit_code"),
                "timed_out": output_value.get("timed_out"),
                "execution_id_present": bool(output_value.get("execution_id")),
                "snapshot_id_present": bool(output_value.get("snapshot_id")),
                "evidence_contract": evidence.get("evidence_contract"),
                "evidence_files": len(evidence.get("files") or []),
                "evidence_symbols": len(evidence.get("symbols") or []),
                "failure_memory_recorded": bool(failure_memory.get("recorded") or failure_memory.get("signature")),
            }
        )
    if calls:
        return {"count": len(calls), "calls": calls}
    return {"count": turn.tools.count("execute_sandbox"), "calls": []}


def extract_context_payload_tokens(events: list[Any], raw: str) -> int:
    payloads: list[str] = []
    for event in events:
        for node in iter_json_nodes(event):
            if not isinstance(node, dict):
                continue
            tool_name = node.get("tool")
            if tool_name not in {"query_context", "graphkv-binary-star_query_context"}:
                continue
            state = node.get("state")
            if not isinstance(state, dict):
                continue
            output = state.get("output")
            if not isinstance(output, str) or not output:
                continue
            context_text = output
            try:
                parsed_output = json.loads(output)
            except json.JSONDecodeError:
                parsed_output = None
            if isinstance(parsed_output, dict):
                parsed_context = parsed_output.get("context")
                if isinstance(parsed_context, str) and parsed_context:
                    context_text = parsed_context
            payloads.append(context_text)
    return sum(estimate_tokens(payload) for payload in payloads)


def extract_llm_tokens(events: list[Any]) -> dict[str, int]:
    totals = {"total": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    for event in events:
        for node in iter_json_nodes(event):
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


def run_opencode(root: Path, message: str, timeout_seconds: int, title: str, label: str, marker: str, stream_logs: bool) -> TurnResult:
    args = ["opencode", "run", "--format", "json", "--print-logs", "--agent", "build", "--dir", str(root), "--title", title, message]
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("OPENCODE_DISABLE_AUTO_UPDATE", "1")
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    command = run_streaming(args, root, timeout_seconds, env, stream_logs)
    events = parse_json_events(command.combined)
    tools, native_tools = extract_tools(events, command.combined)
    assert_no_hesitation_violation(tools)
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


def run_opencode_trial(root: Path, message: str, timeout_seconds: int, title: str, label: str, marker: str, stream_logs: bool) -> TurnResult:
    started = time.monotonic()
    try:
        return run_opencode(root, message, timeout_seconds, title, label, marker, stream_logs)
    except Exception as error:
        command = CommandResult(
            args=["opencode", "run", "--agent", "build", "--dir", root.as_posix(), label],
            returncode=-1,
            stdout="",
            stderr=f"{type(error).__name__}: {error}",
            duration_seconds=time.monotonic() - started,
        )
        return TurnResult(
            label=label,
            command=command,
            events=[],
            tools=[],
            native_tools=[],
            context_payload_tokens=0,
            llm_tokens={"total": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0},
            marker_seen=False,
        )


def opencode_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("OPENCODE_DISABLE_AUTO_UPDATE", "1")
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return env


def run_quiet(args: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str] | None = None) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    return CommandResult(args, completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started)


def clone_or_update_repo(spec: RepoSpec, suite_dir: Path, timeout_seconds: int, refresh: bool) -> Path:
    suite_dir.mkdir(parents=True, exist_ok=True)
    repo = suite_dir / spec.name
    if refresh and repo.exists():
        shutil.rmtree(repo)
    if not repo.exists():
        result = run_quiet(["git", "clone", "--depth", "1", spec.url, repo.as_posix()], suite_dir, timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError(f"failed to clone {spec.url}:\n{result.combined[-4000:]}")
    elif (repo / ".git").is_dir():
        run_quiet(["git", "fetch", "--depth", "1", "origin", "HEAD"], repo, min(timeout_seconds, 180))
    return repo


def resolve_repos(graphkv_root: Path, repos: list[Path], suite: str | None, suite_dir: Path | None, repo_limit: int | None, clone_timeout: int, refresh: bool) -> list[Path]:
    resolved = [repo.resolve() for repo in repos]
    if suite is None:
        if not resolved:
            raise RuntimeError("provide at least one repository path or pass --suite complex")
        return resolved
    if suite != "complex":
        raise RuntimeError(f"unknown suite {suite!r}; supported suites: complex")
    specs = list(COMPLEX_REPO_SUITE[: repo_limit or len(COMPLEX_REPO_SUITE)])
    target_dir = (suite_dir or graphkv_root / "benchmarks/data/local_repos/complex").resolve()
    resolved.extend(clone_or_update_repo(spec, target_dir, clone_timeout, refresh) for spec in specs)
    return resolved


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
    env = opencode_env()
    mcp_list = run_quiet(["opencode", "mcp", "list"], repo, timeout_seconds, env)
    probes: dict[str, str] = {}
    for tool in ("read", "bash", "grep", "glob", "webfetch", "websearch", "codesearch", "apply_patch"):
        probe = run_quiet(["opencode", "debug", "agent", "build", "--tool", tool, "--params", "{}"], repo, timeout_seconds, env)
        probes[tool] = (probe.stdout + probe.stderr).strip().splitlines()[-1] if (probe.stdout + probe.stderr).strip() else ""
    isolated = "graphkv-binary-star" in mcp_list.combined and "1 server(s)" in mcp_list.combined
    native_probes_blocked = all("disabled for agent build" in line or "not found for agent build" in line for line in probes.values())
    return {
        "isolated": isolated,
        "mcp_list": mcp_list.combined,
        "native_probe_last_lines": probes,
        "native_probes_blocked": native_probes_blocked,
    }


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
        "sandbox_usage": sandbox_usage(turn),
    }


def scenario_list(value: str) -> list[str]:
    scenarios = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(scenarios) - set(DEFAULT_SCENARIOS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown scenarios: {', '.join(unknown)}")
    return scenarios


def survey_prompt(repo_name: str, marker: str) -> str:
    return (
        "SYSTEM OVERRIDE: You are blind to this repo except for graphkv-binary-star MCP tools. "
        f"For repository {repo_name}, call query_context with a broad architecture query, then call read_directory_lens('.') once. "
        "Use only that compact evidence to explain the main subsystems, one plausible useful next engineering action, "
        f"and how this avoided raw-repo token burn. Finish with {marker}."
    )


def proof_edit_prompt(marker: str) -> str:
    return (
        "Use only graphkv-binary-star MCP tools to do a concrete disposable repo-local edit. "
        f"Target {PROBE_FILE}. First call query_context for the file and the summarize_repository_signal function. "
        "If query_context returns exact evidence_id and range_sha256 coordinates for summarize_repository_signal, edit immediately; otherwise call search_source for summarize_repository_signal and trust its coordinates. "
        "Pass the selected evidence_id as evidence_id and range_sha256 as expected_range_sha256 to edit_source_code. "
        "Immediately call edit_source_code so summarize_repository_signal returns exactly this shape: "
        "f\"repo={normalized};sha256={digest};len={len(repository_name)}\". "
        "Do not call read_directory_lens, read_source_slice, or execute_sandbox before edit_source_code. "
        "Use the edit_source_code edit_proof_id as patch_id when calling execute_sandbox. "
        f"Finally call execute_sandbox with exactly: python3 -m py_compile {PROBE_FILE} && python3 {PROBE_FILE}. "
        "In your final answer, explicitly report the sandbox execution_id, snapshot_id, evidence, and failure_memory fields. "
        f"Finish with {marker}."
    )


def failure_recovery_prompt(marker: str) -> str:
    return (
        "Use only graphkv-binary-star MCP tools to repair an execution-discovered failure. "
        f"First call execute_sandbox with exactly: python3 {FAILURE_PROBE_FILE}. "
        "Use the returned execution evidence, stack trace, and failure_signature as the bug report. "
        "Then call rank_intent_targets exactly once with that failure evidence and call query_context for the top implicated target. "
        "If query_context returns exact evidence_id and range_sha256 coordinates for normalize_failure_label, edit immediately; otherwise call search_source for normalize_failure_label. "
        "Immediately call edit_source_code with the selected evidence_id and range_sha256 as expected_range_sha256. "
        "Fix normalize_failure_label so None or an empty name becomes unknown-repository while preserving strip/lower/underscore/space normalization for real names. "
        "Use the edit_source_code edit_proof_id as patch_id when calling the validation sandbox. "
        f"Finally call execute_sandbox with exactly: python3 -m py_compile {FAILURE_PROBE_FILE} && python3 {FAILURE_PROBE_FILE}. "
        "In your final answer, explicitly report both sandbox execution_id values, both snapshot_id values, the validation evidence, and failure_memory fields. "
        f"Finish with {marker}."
    )


def verify_probe_contracts(repo: Path) -> dict[str, Any]:
    proof_compile = run_quiet([sys.executable, "-m", "py_compile", PROBE_FILE], repo, 60)
    proof_run = run_quiet([sys.executable, PROBE_FILE], repo, 60)
    failure_compile = run_quiet([sys.executable, "-m", "py_compile", FAILURE_PROBE_FILE], repo, 60)
    failure_run = run_quiet([sys.executable, FAILURE_PROBE_FILE], repo, 60)
    proof_text = (repo / PROBE_FILE).read_text(encoding="utf-8", errors="replace")
    failure_text = (repo / FAILURE_PROBE_FILE).read_text(encoding="utf-8", errors="replace")
    return {
        "proof_probe": {
            "ok": proof_compile.returncode == 0 and proof_run.returncode == 0 and "sha256=" in proof_run.stdout and "len=" in proof_run.stdout,
            "compile_rc": proof_compile.returncode,
            "run_rc": proof_run.returncode,
            "stdout": proof_run.stdout.strip()[-300:],
            "text_has_expected_shape": "len(repository_name)" in proof_text and "sha256" in proof_text,
        },
        "failure_probe": {
            "ok": failure_compile.returncode == 0 and failure_run.returncode == 0 and "unknown-repository" in failure_run.stdout,
            "compile_rc": failure_compile.returncode,
            "run_rc": failure_run.returncode,
            "stdout": failure_run.stdout.strip()[-300:],
            "text_has_expected_fix": "unknown-repository" in failure_text,
        },
    }


def repo_failure_reasons(repo_report: dict[str, Any], scenarios: list[str]) -> list[str]:
    reasons: list[str] = []
    if not repo_report.get("lockdown", {}).get("isolated"):
        reasons.append("OpenCode did not report isolated graphkv-binary-star MCP")
    if not repo_report.get("lockdown", {}).get("native_probes_blocked"):
        reasons.append("native debug probes were not fully blocked")
    if "survey" in scenarios and not repo_report.get("survey_expected_tools"):
        reasons.append("survey did not use expected query_context + read_directory_lens tools")
    if "proof_edit" in scenarios:
        if not repo_report.get("proof_edit_expected_tools"):
            reasons.append("proof edit did not use query_context/edit_source_code/execute_sandbox")
        if not repo_report.get("proof_edit_pipeline_ok"):
            reasons.append("proof edit violated direct source-coordinate edit pipeline")
        if not all(repo_report.get("work_proof_markers", {}).values()):
            reasons.append("proof edit did not expose every proof marker")
        if not all(repo_report.get("work_execution_markers", {}).values()):
            reasons.append("proof edit did not expose every execution evidence marker")
        if not repo_report.get("probe_contracts", {}).get("proof_probe", {}).get("ok"):
            reasons.append("proof probe did not compile/run with expected sha256+len output")
    if "failure_recovery" in scenarios:
        if not repo_report.get("failure_recovery_expected_tools"):
            reasons.append("failure recovery did not use execute_sandbox/rank_intent_targets/query_context/edit_source_code")
        if not repo_report.get("failure_recovery_pipeline_ok"):
            reasons.append("failure recovery violated execution-evidence edit ordering")
        if not repo_report.get("failure_recovery_sandbox_ok"):
            reasons.append("failure recovery did not show both failing and passing sandbox calls")
        if not repo_report.get("probe_contracts", {}).get("failure_probe", {}).get("ok"):
            reasons.append("failure probe did not compile/run with expected unknown-repository output")
    for turn in repo_report.get("turns", []):
        if not turn.get("ok"):
            reasons.append(f"{turn.get('label')} turn failed marker/native/returncode checks")
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MCP-only OpenCode field trials across local repositories.")
    parser.add_argument("repos", nargs="*", type=Path)
    parser.add_argument("--graphkv-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--suite", choices=("complex",), default=None, help="Clone and run a built-in complex repository suite.")
    parser.add_argument("--suite-dir", type=Path, default=None, help="Directory for cloned suite repositories.")
    parser.add_argument("--repo-limit", type=int, default=None, help="Limit the number of built-in suite repositories.")
    parser.add_argument("--refresh-suite", action="store_true", help="Delete and reclone built-in suite repositories before running.")
    parser.add_argument("--clone-timeout-seconds", type=int, default=600)
    parser.add_argument("--scenarios", type=scenario_list, default=list(DEFAULT_SCENARIOS), help="Comma-separated scenarios: survey,proof_edit,failure_recovery")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--timeout-seconds", type=int, default=360)
    parser.add_argument("--max-files", type=int, default=350)
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024)
    parser.add_argument("--lens-depth", type=int, default=3)
    parser.add_argument("--lens-entries", type=int, default=450)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-restore-opencode", action="store_true")
    parser.add_argument("--keep-probes", action="store_true", help="Leave disposable probe files in each target repo after the run.")
    parser.add_argument("--stream-logs", action="store_true")
    args = parser.parse_args()

    graphkv_root = args.graphkv_root.resolve()
    scenarios = args.scenarios or list(DEFAULT_SCENARIOS)
    target_repos = resolve_repos(
        graphkv_root,
        args.repos,
        args.suite,
        args.suite_dir,
        args.repo_limit,
        args.clone_timeout_seconds,
        args.refresh_suite,
    )
    output_dir = graphkv_root / "benchmarks/results/field-trials"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output or output_dir / f"opencode_field_trials_{int(time.time())}.json"
    backup = backup_opencode_config()
    report: dict[str, Any] = {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "opencode_backup": backup.as_posix(),
        "suite": args.suite,
        "suite_repos": [
            {"name": spec.name, "url": spec.url, "description": spec.description}
            for spec in COMPLEX_REPO_SUITE[: args.repo_limit or len(COMPLEX_REPO_SUITE)]
        ] if args.suite == "complex" else [],
        "scenarios": scenarios,
        "repos": [],
    }

    try:
        for repo in target_repos:
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
            title = f"GraphKV field trial {repo_name} {int(time.time())}"
            probe_snapshots = seed_probe_files(repo)
            turns: list[TurnResult] = []
            try:
                survey: TurnResult | None = None
                proof_edit: TurnResult | None = None
                failure_recovery: TurnResult | None = None
                if "survey" in scenarios:
                    survey_marker = "GRAPHKV_SURVEY_COMPLETE"
                    survey = run_opencode_trial(repo, survey_prompt(repo_name, survey_marker), args.timeout_seconds, title, "survey", survey_marker, args.stream_logs)
                    turns.append(survey)
                if "proof_edit" in scenarios:
                    proof_marker = "GRAPHKV_PROOF_EDIT_COMPLETE"
                    proof_edit = run_opencode_trial(repo, proof_edit_prompt(proof_marker), args.timeout_seconds, title, "proof_edit", proof_marker, args.stream_logs)
                    turns.append(proof_edit)
                if "failure_recovery" in scenarios:
                    failure_marker = "GRAPHKV_FAILURE_RECOVERY_COMPLETE"
                    failure_recovery = run_opencode_trial(repo, failure_recovery_prompt(failure_marker), args.timeout_seconds, title, "failure_recovery", failure_marker, args.stream_logs)
                    turns.append(failure_recovery)

                survey_expected_tools = survey is None or {"query_context", "read_directory_lens"}.issubset(set(survey.tools))
                proof_edit_expected_tools = proof_edit is None or {"query_context", "edit_source_code", "execute_sandbox"}.issubset(set(proof_edit.tools))
                proof_edit_pipeline_ok = proof_edit is None or proof_edit_pipeline_ok_tools(proof_edit.tools)
                work_proof_markers = proof_markers_seen(proof_edit) if proof_edit else {marker: True for marker in PROOF_MARKERS}
                work_execution_markers = execution_markers_seen(proof_edit) if proof_edit else {marker: True for marker in EXECUTION_EVIDENCE_MARKERS}

                failure_recovery_expected_tools = failure_recovery is None or {
                    "execute_sandbox",
                    "rank_intent_targets",
                    "query_context",
                    "edit_source_code",
                }.issubset(set(failure_recovery.tools))
                failure_recovery_pipeline_ok = failure_recovery is None or failure_recovery_pipeline_ok_tools(failure_recovery.tools)
                failure_sandbox = sandbox_usage(failure_recovery) if failure_recovery else {"count": 0, "calls": []}
                failure_calls = failure_sandbox.get("calls", []) if isinstance(failure_sandbox, dict) else []
                probe_contracts = verify_probe_contracts(repo)
                failure_recovery_sandbox_ok = failure_recovery is None or (
                    failure_sandbox.get("count", 0) >= 2
                    and any(call.get("ok") is False for call in failure_calls)
                    and (
                        any(call.get("ok") is True for call in failure_calls)
                        or probe_contracts.get("failure_probe", {}).get("ok")
                    )
                )
            finally:
                if not args.keep_probes:
                    restore_probe_files(repo, probe_snapshots)
            repo_report = {
                "repo": repo.as_posix(),
                "head": run_quiet(["git", "rev-parse", "--short", "HEAD"], repo, 30).stdout.strip(),
                "raw_text_files": file_count,
                "raw_text_bytes": byte_count,
                "raw_text_tokens_estimate": raw_tokens,
                "skipped_tracked_files": skipped_count,
                "ingest": ingest,
                "lockdown": lockdown,
                "probe_contracts": probe_contracts,
                "survey_expected_tools": survey_expected_tools,
                "turns": [turn_report(turn) for turn in turns],
                "proof_edit_expected_tools": proof_edit_expected_tools,
                "work_proof_markers": work_proof_markers,
                "work_execution_markers": work_execution_markers,
                "proof_edit_pipeline_ok": proof_edit_pipeline_ok,
                "failure_recovery_expected_tools": failure_recovery_expected_tools,
                "failure_recovery_pipeline_ok": failure_recovery_pipeline_ok,
                "failure_recovery_sandbox_ok": failure_recovery_sandbox_ok,
            }
            mcp_tokens = sum(turn["context_payload_tokens"] for turn in repo_report["turns"])
            repo_report["mcp_context_tokens_total"] = mcp_tokens
            repo_report["raw_to_mcp_context_reduction_ratio"] = round(raw_tokens / max(1, mcp_tokens), 2)
            repo_report["raw_to_mcp_context_savings_percent"] = round((1 - (mcp_tokens / max(1, raw_tokens))) * 100, 2)
            selected_probe_contracts = []
            if "proof_edit" in scenarios:
                selected_probe_contracts.append(repo_report["probe_contracts"]["proof_probe"]["ok"])
            if "failure_recovery" in scenarios:
                selected_probe_contracts.append(repo_report["probe_contracts"]["failure_probe"]["ok"])
            repo_report["behavior_ok"] = (
                all(turn["ok"] for turn in repo_report["turns"])
                and all(selected_probe_contracts or [True])
                and lockdown["isolated"]
                and lockdown["native_probes_blocked"]
            )
            repo_report["protocol_ok"] = (
                survey_expected_tools
                and proof_edit_expected_tools
                and proof_edit_pipeline_ok
                and all(work_proof_markers.values())
                and all(work_execution_markers.values())
                and failure_recovery_expected_tools
                and failure_recovery_pipeline_ok
                and failure_recovery_sandbox_ok
            )
            repo_report["failure_reasons"] = repo_failure_reasons(repo_report, scenarios)
            repo_report["ok"] = not repo_report["failure_reasons"]
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