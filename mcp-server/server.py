#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import docker
import httpx
import nats
import websockets
from docker.client import DockerClient
from mcp.server.fastmcp import Context, FastMCP


DEFAULT_GRAPHKV_WS_URL = "ws://127.0.0.1:8888/v1/ingest"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"
MAX_LOG_LINES = 2000
WEB_DOCUMENTATION_SUBJECT = "graphkv.documentation.ingest.requests"
SANDBOX_LOG_SUBJECT = "graphkv.sandbox.logs.ingest.requests"


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: int
    scopes: tuple[str, ...]
    workflow_token: str | None
    token_hash: str | None

    def public_claims(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "scopes": list(self.scopes),
            "token_hash": self.token_hash,
        }


class OAuthWorkflowTokenValidator:
    def __init__(self) -> None:
        self.require_auth = os.getenv("GRAPHKV_MCP_REQUIRE_AUTH", "false").lower() in {"1", "true", "yes"}
        self.local_user = os.getenv("GRAPHKV_MCP_LOCAL_USER", "local-workflow")
        self.introspection_url = os.getenv("GRAPHKV_MCP_AUTH_INTROSPECTION_URL")
        self.trusted_tokens_file = os.getenv("GRAPHKV_MCP_TRUSTED_TOKENS_FILE")
        self.local_workflow_token = os.getenv("GRAPHKV_MCP_ACCESS_TOKEN")

    async def validate(self, context: Context | None, required_scopes: tuple[str, ...]) -> AuthContext:
        token = self._extract_bearer_token(context) or self.local_workflow_token
        if token and self.introspection_url:
            return await self._validate_by_introspection(token, required_scopes)
        if token and self.trusted_tokens_file:
            return self._validate_by_trusted_file(token, required_scopes)
        if self.require_auth:
            raise PermissionError("missing OAuth workflow token for GraphKV MCP request")
        return self._local_context(token, required_scopes)

    def _extract_bearer_token(self, context: Context | None) -> str | None:
        if context is None:
            return None
        request_context = getattr(context, "request_context", None)
        candidates = [
            getattr(request_context, "headers", None),
            getattr(request_context, "metadata", None),
            getattr(request_context, "meta", None),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            authorization = candidate.get("authorization") or candidate.get("Authorization")
            if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
                return authorization[7:].strip()
            token = candidate.get("access_token") or candidate.get("oauth_token")
            if isinstance(token, str) and token:
                return token
        return None

    async def _validate_by_introspection(self, token: str, required_scopes: tuple[str, ...]) -> AuthContext:
        assert self.introspection_url is not None
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(self.introspection_url, data={"token": token})
            response.raise_for_status()
            claims = response.json()
        if not claims.get("active"):
            raise PermissionError("OAuth workflow token is inactive")
        scopes = tuple(str(claim) for claim in str(claims.get("scope", "")).split() if claim)
        self._ensure_scopes(scopes, required_scopes)
        user_id = str(claims.get("sub") or claims.get("username") or "workflow")
        tenant_id = self._tenant_id_for(user_id)
        return AuthContext(user_id, tenant_id, scopes, token, self._hash_token(token))

    def _validate_by_trusted_file(self, token: str, required_scopes: tuple[str, ...]) -> AuthContext:
        assert self.trusted_tokens_file is not None
        data = json.loads(Path(self.trusted_tokens_file).read_text(encoding="utf-8"))
        claim = data.get(token)
        if not isinstance(claim, dict):
            raise PermissionError("OAuth workflow token is not trusted")
        scopes = tuple(str(scope) for scope in claim.get("scopes", []))
        self._ensure_scopes(scopes, required_scopes)
        user_id = str(claim.get("user_id") or "workflow")
        tenant_id = int(claim.get("tenant_id") or self._tenant_id_for(user_id))
        return AuthContext(user_id, tenant_id, scopes, token, self._hash_token(token))

    def _local_context(self, token: str | None, required_scopes: tuple[str, ...]) -> AuthContext:
        scopes = ("local", *required_scopes)
        return AuthContext(
            user_id=self.local_user,
            tenant_id=self._tenant_id_for(self.local_user),
            scopes=scopes,
            workflow_token=token,
            token_hash=self._hash_token(token) if token else None,
        )

    def _ensure_scopes(self, scopes: tuple[str, ...], required_scopes: tuple[str, ...]) -> None:
        missing = sorted(set(required_scopes) - set(scopes))
        if missing:
            raise PermissionError(f"OAuth workflow token is missing scopes: {', '.join(missing)}")

    @staticmethod
    def _tenant_id_for(user_id: str) -> int:
        digest = hashlib.sha256(user_id.encode("utf-8")).digest()
        return max(1, int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


class NatsPool:
    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> Any:
        async with self._lock:
            if self._client is None or getattr(self._client, "is_closed", False):
                if not await self._server_reachable():
                    raise ConnectionError(f"NATS is not reachable at {self.url}")
                self._client = await nats.connect(servers=[self.url], allow_reconnect=False, max_reconnect_attempts=0)
            return self._client

    async def _server_reachable(self) -> bool:
        parsed = urlparse(self.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 4222
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.5)
        except (OSError, TimeoutError):
            return False
        writer.close()
        await writer.wait_closed()
        return True

    async def publish_json(self, subject: str, payload: dict[str, Any]) -> None:
        client = await self.client()
        await client.publish(subject, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        await client.flush()

    async def close(self) -> None:
        if self._client is not None and not getattr(self._client, "is_closed", True):
            await self._client.drain()


class GraphKVProxy:
    def __init__(self, query_ws_url: str, nats_pool: NatsPool) -> None:
        self.query_ws_url = query_ws_url
        self.nats_pool = nats_pool

    async def query_context(
        self,
        auth: AuthContext,
        query: str,
        target_node: str | None,
        depth: int,
    ) -> dict[str, Any]:
        enriched_query = self._format_graph_query(query, target_node, depth)
        async with websockets.connect(self.query_ws_url, max_size=32 * 1024 * 1024) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "auth",
                        "token": auth.workflow_token or f"workflow:{auth.tenant_id}",
                        "tenant_id": auth.tenant_id,
                    }
                )
            )
            auth_reply = json.loads(await websocket.recv())
            if not auth_reply.get("ok"):
                raise RuntimeError(f"GraphKV auth failed: {auth_reply}")

            message_id = f"mcp-{uuid.uuid4().hex}"
            await websocket.send(
                json.dumps(
                    {
                        "type": "synthesize",
                        "session_id": f"mcp-{auth.tenant_id}",
                        "message_id": message_id,
                        "agent": "graphkv-mcp",
                        "provider_id": os.getenv("GRAPHKV_MCP_PROVIDER_ID", "anthropic"),
                        "model_id": os.getenv("GRAPHKV_MCP_MODEL_ID", "claude-sonnet-4"),
                        "query": enriched_query,
                    }
                )
            )
            reply = json.loads(await websocket.recv())
        if not reply.get("ok"):
            raise RuntimeError(f"GraphKV query failed: {reply}")
        return {
            "ok": True,
            "query": query,
            "target_node": target_node,
            "depth": depth,
            "graphkv": reply,
            "text": self._first_opencode_text(reply),
        }

    async def ingest_web_documentation(self, auth: AuthContext, url: str, context_tag: str) -> dict[str, Any]:
        request_id = f"webdoc-{uuid.uuid4().hex}"
        payload = {
            "schema_version": 1,
            "kind": "web_documentation_ingest",
            "request_id": request_id,
            "url": url,
            "context_tag": context_tag,
            "auth": auth.public_claims(),
            "requested_at": datetime.now(UTC).isoformat(),
        }
        await self.nats_pool.publish_json(WEB_DOCUMENTATION_SUBJECT, payload)
        return {"ok": True, "request_id": request_id, "subject": WEB_DOCUMENTATION_SUBJECT}

    async def ingest_sandbox_logs(self, auth: AuthContext, command: str, result: dict[str, Any]) -> None:
        try:
            await self.nats_pool.publish_json(
                SANDBOX_LOG_SUBJECT,
                {
                    "schema_version": 1,
                    "kind": "sandbox_execution_logs",
                    "request_id": f"sandbox-{uuid.uuid4().hex}",
                    "command": command,
                    "auth": auth.public_claims(),
                    "result": result,
                    "requested_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception:
            return

    @staticmethod
    def _format_graph_query(query: str, target_node: str | None, depth: int) -> str:
        if target_node is None:
            return query
        return f"{query}\ntarget_node: {target_node}\ndepth: {depth}"

    @staticmethod
    def _first_opencode_text(reply: dict[str, Any]) -> str:
        parts = reply.get("opencode", {}).get("body", {}).get("parts", [])
        if not parts or not isinstance(parts[0], dict):
            return ""
        text = parts[0].get("text", "")
        return text if isinstance(text, str) else ""


class DockerSandbox:
    def __init__(self, client: DockerClient, workspace_root: Path, image: str) -> None:
        self.client = client
        self.workspace_root = workspace_root
        self.image = image

    async def run(self, command: str, timeout_seconds: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_blocking, command, timeout_seconds)

    def _run_blocking(self, command: str, timeout_seconds: int) -> dict[str, Any]:
        timeout_seconds = max(1, min(timeout_seconds, 600))
        with tempfile.TemporaryDirectory(prefix="graphkv-mcp-sandbox-") as output_directory:
            output_path = Path(output_directory)
            script = self._sandbox_script(command, timeout_seconds)
            docker_error: str | None = None
            try:
                self.client.containers.run(
                    self.image,
                    ["/bin/bash", "-lc", script],
                    auto_remove=True,
                    detach=False,
                    stdout=True,
                    stderr=True,
                    working_dir="/workspace",
                    volumes={
                        str(self.workspace_root): {"bind": "/workspace", "mode": "rw"},
                        str(output_path): {"bind": "/outputs", "mode": "rw"},
                    },
                )
            except Exception as error:  # Docker SDK wraps daemon and image errors in several subclasses.
                docker_error = str(error)

            stdout = self._read_output(output_path / "stdout.log")
            stderr = self._read_output(output_path / "stderr.log")
            exit_code_text = self._read_output(output_path / "exit_code").strip()
            exit_code = int(exit_code_text) if exit_code_text.isdigit() else None
            return {
                "ok": docker_error is None and exit_code == 0,
                "image": self.image,
                "command": command,
                "timeout_seconds": timeout_seconds,
                "timed_out": exit_code == 124,
                "exit_code": exit_code,
                "stdout": truncate_lines(stdout),
                "stderr": truncate_lines(stderr),
                "docker_error": docker_error,
            }

    @staticmethod
    def _sandbox_script(command: str, timeout_seconds: int) -> str:
        quoted_command = shlex.quote(command)
        return "\n".join(
            [
                "set +e",
                f"timeout {timeout_seconds}s /bin/bash -lc {quoted_command} > /outputs/stdout.log 2> /outputs/stderr.log",
                "status=$?",
                "printf '%s' \"$status\" > /outputs/exit_code",
                "exit 0",
            ]
        )

    @staticmethod
    def _read_output(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")


@dataclass
class AppState:
    workspace_root: Path
    docker_client: DockerClient
    nats_pool: NatsPool
    graphkv: GraphKVProxy
    sandbox: DockerSandbox
    auth: OAuthWorkflowTokenValidator

    @classmethod
    def from_environment(cls) -> AppState:
        workspace_root = Path(os.getenv("GRAPHKV_WORKSPACE_ROOT", Path.cwd())).resolve()
        docker_client = docker.from_env()
        nats_pool = NatsPool(os.getenv("GRAPHKV_NATS_URL", DEFAULT_NATS_URL))
        graphkv = GraphKVProxy(os.getenv("GRAPHKV_QUERY_WS_URL", DEFAULT_GRAPHKV_WS_URL), nats_pool)
        sandbox = DockerSandbox(
            docker_client,
            workspace_root,
            os.getenv("GRAPHKV_MCP_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE),
        )
        return cls(workspace_root, docker_client, nats_pool, graphkv, sandbox, OAuthWorkflowTokenValidator())

    async def close(self) -> None:
        await self.nats_pool.close()
        self.docker_client.close()


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppState]:
    state = AppState.from_environment()
    try:
        yield state
    finally:
        await state.close()


mcp = FastMCP("GraphKV Binary Star", lifespan=app_lifespan)


def state_from(context: Context) -> AppState:
    return context.request_context.lifespan_context


async def authorize(context: Context, *scopes: str) -> tuple[AppState, AuthContext]:
    state = state_from(context)
    auth = await state.auth.validate(context, tuple(scopes))
    return state, auth


def resolve_workspace_path(root: Path, path: str) -> Path:
    requested = Path(path).expanduser()
    candidate = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path escapes GraphKV workspace jail: {path}") from error
    return candidate


def truncate_lines(text: str, max_lines: int = MAX_LOG_LINES) -> dict[str, Any]:
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    return {
        "line_count": len(lines),
        "truncated": truncated,
        "text": "\n".join(lines[:max_lines]),
    }


def directory_tree(path: Path, root: Path, depth: int, budget: list[int]) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix() if path != root else "."
    if path.is_file():
        return {"type": "file", "path": relative, "bytes": path.stat().st_size}
    node: dict[str, Any] = {"type": "directory", "path": relative, "children": []}
    if depth <= 0:
        node["truncated"] = True
        return node
    blocked = {
        ".cache",
        ".git",
        "_deps",
        "build",
        "build-benchmark",
        "build-core",
        "build-doc-plane",
        "build-impl",
        "build-impl-server",
        "build-require-folly",
        "CMakeFiles",
        "node_modules",
        "target",
        "tmp",
        "uploads",
    }
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if budget[0] <= 0:
            node["truncated"] = True
            break
        budget[0] -= 1
        if child.name in blocked and child.is_dir():
            node["children"].append({"type": "directory", "path": child.relative_to(root).as_posix(), "truncated": True})
            continue
        node["children"].append(directory_tree(child, root, depth - 1, budget))
    return node


async def run_git(root: Path, args: list[str]) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "git",
        "--no-pager",
        *args,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def observe_background_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except Exception as error:
        print(f"GraphKV background ingest failed: {error}", flush=True)


@mcp.tool()
async def query_context(
    query: str,
    target_node: str | None = None,
    depth: int = 1,
    ctx: Context = None,
) -> dict[str, Any]:
    """query_context(query: str, target_node: str = None, depth: int = 1): Forwards the query to the GraphKV engine and returns the structural/semantic payload."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    if depth < 0 or depth > 64:
        raise ValueError("depth must be between 0 and 64")
    state, auth = await authorize(ctx, "graphkv:query")
    return await state.graphkv.query_context(auth, query, target_node, depth)


@mcp.tool()
async def read_directory_lens(path: str, ctx: Context = None) -> dict[str, Any]:
    """read_directory_lens(path: str): Returns a read-only JSON tree of the directory structure. Jails the path to the project root."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:read")
    target = resolve_workspace_path(state.workspace_root, path)
    if not target.exists():
        raise FileNotFoundError(path)
    max_depth = int(os.getenv("GRAPHKV_MCP_LENS_DEPTH", "3"))
    max_entries = int(os.getenv("GRAPHKV_MCP_LENS_MAX_ENTRIES", "600"))
    return {"ok": True, "root": state.workspace_root.as_posix(), "tree": directory_tree(target, state.workspace_root, max_depth, [max_entries])}


@mcp.tool()
async def execute_sandbox(command: str, timeout_seconds: int = 30, ctx: Context = None) -> dict[str, Any]:
    """execute_sandbox(command: str, timeout_seconds: int = 30): Executes the Docker sandbox logic. Returns the truncated logs and automatically triggers a background task to ingest those logs into GraphKV."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, auth = await authorize(ctx, "workspace:execute")
    result = await state.sandbox.run(command, timeout_seconds)
    task = asyncio.create_task(state.graphkv.ingest_sandbox_logs(auth, command, result))
    task.add_done_callback(observe_background_task)
    return result


@mcp.tool()
async def edit_source_code(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    ctx: Context = None,
) -> dict[str, Any]:
    """edit_source_code(path: str, start_line: int, end_line: int, new_content: str): Reads the file, replaces the exact line range, and writes it back."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:edit")
    target = resolve_workspace_path(state.workspace_root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    if start_line < 1 or end_line < start_line:
        raise ValueError("line range must be 1-indexed and inclusive")
    original = target.read_text(encoding="utf-8")
    original_lines = original.splitlines(keepends=True)
    if end_line > len(original_lines):
        raise ValueError(f"end_line {end_line} exceeds file length {len(original_lines)}")
    replacement = new_content.splitlines(keepends=True)
    updated_lines = original_lines[: start_line - 1] + replacement + original_lines[end_line:]
    updated = "".join(updated_lines)
    target.write_text(updated, encoding="utf-8")
    return {
        "ok": True,
        "path": target.relative_to(state.workspace_root).as_posix(),
        "start_line": start_line,
        "end_line": end_line,
        "old_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "new_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
    }


@mcp.tool()
async def ingest_web_documentation(url: str, context_tag: str, ctx: Context = None) -> dict[str, Any]:
    """ingest_web_documentation(url: str, context_tag: str): Forwards the URL to the Rust Warden for Docling parsing and vLLM embedding."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be http or https")
    state, auth = await authorize(ctx, "graphkv:ingest")
    return await state.graphkv.ingest_web_documentation(auth, url, context_tag)


@mcp.tool()
async def inspect_git_state(action: str, target_file: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """inspect_git_state(action: str, target_file: str = None): Executes git status or git diff using standard subprocess calls."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:read")
    if action not in {"status", "diff"}:
        raise ValueError("action must be 'status' or 'diff'")
    relative_target: str | None = None
    if target_file is not None:
        target = resolve_workspace_path(state.workspace_root, target_file)
        relative_target = target.relative_to(state.workspace_root).as_posix()
    if action == "status":
        args = ["status", "--short"]
        if relative_target is not None:
            args.extend(["--", relative_target])
    else:
        args = ["diff"]
        if relative_target is not None:
            args.extend(["--", relative_target])
    result = await run_git(state.workspace_root, args)
    result["action"] = action
    result["target_file"] = relative_target
    return result


if __name__ == "__main__":
    mcp.run(transport=os.getenv("GRAPHKV_MCP_TRANSPORT", "stdio"))