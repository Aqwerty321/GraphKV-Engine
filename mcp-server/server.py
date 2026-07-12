#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
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
DEFAULT_SANDBOX_SNAPSHOT_MODE = "copy"
MAX_LOG_LINES = 2000
MAX_EXECUTION_EVIDENCE_TEXT = 24_000
WEB_DOCUMENTATION_SUBJECT = "graphkv.documentation.ingest.requests"
SANDBOX_LOG_SUBJECT = "graphkv.sandbox.logs.ingest.requests"
SOURCE_SKIP_DIRS = {
    ".cache",
    ".graphkv",
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
    "_deps",
    "__pycache__",
    "build",
    "build-benchmark",
    "build-core",
    "build-doc-plane",
    "build-impl",
    "build-impl-server",
    "build-require-folly",
    "CMakeFiles",
    "coverage",
    "dist",
    "field-trials",
    "htmlcov",
    "node_modules",
    "target",
    "tmp",
    "uploads",
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
    ".json",
    ".jsonc",
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
    ".toml",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
MAX_SOURCE_FILE_BYTES = 512 * 1024
MAX_SOURCE_SLICE_LINES = 240
MAX_SOURCE_SEARCH_FILES = 1500
MAX_SOURCE_SEARCH_RESULTS = 80
MAX_INTENT_ROUTER_RESULTS = 12
MAX_INTENT_ROUTER_FILES = 1500
MAX_QUERY_CONTEXT_CANDIDATES = 3
MAX_FAILURE_MEMORY_RECORDS = 200
MAX_FAILURE_MEMORY_HITS = 5
INTENT_CONTEXT_LINES = 2
FAILURE_MEMORY_DIR = ".graphkv"
FAILURE_MEMORY_FILE = "failure_memory.jsonl"
CONFIG_DEPENDENCY_FILES = (
    "CMakeLists.txt",
    "Cargo.toml",
    "go.mod",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "tsconfig.json",
)
INTENT_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
SYMBOL_DEFINITION_RE = re.compile(
    r"^\s*(?:(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var|interface|type)\s+|"
    r"(?:pub\s+)?(?:async\s+)?fn\s+|(?:pub\s+)?(?:struct|enum|trait|impl)\s+)([A-Za-z_][A-Za-z0-9_]*)"
)
CPP_FUNCTION_RE = re.compile(
    r"^\s*(?:(?:inline|static|constexpr|consteval|virtual|explicit)\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_:<>~*&]*\s+)+([A-Za-z_~][A-Za-z0-9_:~]*)\s*\([^;]*\)"
    r"(?:\s*(?:const|noexcept|override|final))*\s*(?:\{|$)"
)
COMPLEXITY_RE = re.compile(r"\b(if|elif|else|for|while|case|catch|except|match|switch|try|await|guard)\b|&&|\|\|")
STACK_FRAME_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.\-/]+)\.(?:c|cc|cpp|cs|go|h|hpp|java|js|jsx|kt|lua|php|py|rb|rs|sh|swift|ts|tsx|vue))"
    r"(?::|\(|[\"']?,?\s+line\s+)(?P<line>\d+)"
)
TRACE_FAST_TERMS = frozenset({"crash", "exception", "failed", "failure", "panic", "segfault", "traceback"})
NETWORK_COMMAND_RE = re.compile(r"\b(curl|wget|ssh|scp|rsync|git\s+clone|pip\s+install|npm\s+install|pnpm\s+install|yarn\s+add|cargo\s+fetch|go\s+get)\b")
DEPENDENCY_INSTALL_RE = re.compile(r"\b(apt-get|apk|dnf|yum|brew|pip\s+install|npm\s+install|pnpm\s+install|yarn\s+add|cargo\s+install)\b")
COMPILER_DIAGNOSTIC_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.\-/]+)\.(?:c|cc|cpp|cs|go|h|hpp|java|js|jsx|kt|lua|php|py|rb|rs|sh|swift|ts|tsx|vue))"
    r":(?P<line>\d+)(?::(?P<column>\d+))?:\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>[^\n]+)",
    re.IGNORECASE,
)
PYTEST_FAILURE_RE = re.compile(r"(?:FAILED|ERROR)\s+(?P<path>[A-Za-z0-9_.\-/]+\.py)(?:::(?P<symbol>[A-Za-z_][A-Za-z0-9_:.\[\]-]*))?")
CONTROL_FLOW_PREFIXES = (
    "if ",
    "elif ",
    "else",
    "for ",
    "while ",
    "switch ",
    "case ",
    "catch ",
    "return ",
    "raise ",
    "yield ",
    "await ",
    "with ",
    "assert ",
)
INTENT_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "bug",
        "code",
        "does",
        "error",
        "fix",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "issue",
        "of",
        "on",
        "or",
        "the",
        "thing",
        "this",
        "to",
        "why",
        "where",
        "which",
        "with",
        "work",
        "works",
    }
)


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
        workspace_root: Path,
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
        context_text = self._first_opencode_text(reply)
        graph_status = graph_context_status(context_text)
        local_evidence = query_context_local_evidence(workspace_root, query, target_node, graph_status)
        return {
            "ok": True,
            "target_node": target_node,
            "depth": depth,
            "evidence_contract": "verified_ast_line_coordinates",
            "context": context_text,
            "graph_status": graph_status,
            "local_evidence": local_evidence,
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
    def __init__(self, client: DockerClient, workspace_root: Path, image: str, snapshot_mode: str) -> None:
        self.client = client
        self.workspace_root = workspace_root
        self.image = image
        self.snapshot_mode = snapshot_mode

    async def run(
        self,
        command: str,
        timeout_seconds: int,
        execution_tier: str | None = None,
        patch_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_blocking, command, timeout_seconds, execution_tier, patch_id)

    def _run_blocking(
        self,
        command: str,
        timeout_seconds: int,
        execution_tier: str | None,
        patch_id: str | None,
    ) -> dict[str, Any]:
        started = datetime.now(UTC)
        execution_id = f"exec-{uuid.uuid4().hex}"
        timeout_seconds = max(1, min(timeout_seconds, 600))
        policy = sandbox_policy(command, execution_tier)
        with tempfile.TemporaryDirectory(prefix="graphkv-mcp-sandbox-") as output_directory:
            output_path = Path(output_directory)
            mount_path, snapshot_id, snapshot_cleanup = self._prepare_workspace_mount(output_path, execution_id)
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
                    network_disabled=policy["network_disabled"],
                    volumes={
                        str(mount_path): {"bind": "/workspace", "mode": policy["workspace_mount_mode"]},
                        str(output_path): {"bind": "/outputs", "mode": "rw"},
                    },
                )
            except Exception as error:  # Docker SDK wraps daemon and image errors in several subclasses.
                docker_error = str(error)
            finally:
                snapshot_cleanup()

            stdout = self._read_output(output_path / "stdout.log")
            stderr = self._read_output(output_path / "stderr.log")
            exit_code_text = self._read_output(output_path / "exit_code").strip()
            exit_code = int(exit_code_text) if exit_code_text.isdigit() else None
            completed = datetime.now(UTC)
            result: dict[str, Any] = {
                "ok": docker_error is None and exit_code == 0,
                "execution_id": execution_id,
                "snapshot_id": snapshot_id,
                "snapshot_mode": self.snapshot_mode,
                "patch_id": patch_id,
                "image": self.image,
                "command": command,
                "execution_tier": policy["tier"],
                "execution_policy": policy,
                "timeout_seconds": timeout_seconds,
                "timed_out": exit_code == 124,
                "exit_code": exit_code,
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
                "duration_ms": int((completed - started).total_seconds() * 1000),
                "stdout": truncate_lines(stdout),
                "stderr": truncate_lines(stderr),
                "docker_error": docker_error,
            }
            result["evidence"] = extract_execution_evidence(self.workspace_root, command, result)
            return result

    def _prepare_workspace_mount(self, output_path: Path, execution_id: str) -> tuple[Path, str, Any]:
        if self.snapshot_mode == "bind":
            snapshot_id = workspace_snapshot_id(self.workspace_root, execution_id, "bind")
            return self.workspace_root, snapshot_id, lambda: None
        snapshot_root = output_path / "workspace-snapshot"
        copy_workspace_snapshot(self.workspace_root, snapshot_root)
        snapshot_id = workspace_snapshot_id(self.workspace_root, execution_id, "copy")
        return snapshot_root, snapshot_id, lambda: None

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
            os.getenv("GRAPHKV_MCP_SANDBOX_SNAPSHOT_MODE", DEFAULT_SANDBOX_SNAPSHOT_MODE),
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
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if budget[0] <= 0:
            node["truncated"] = True
            break
        budget[0] -= 1
        if child.name in SOURCE_SKIP_DIRS and child.is_dir():
            node["children"].append({"type": "directory", "path": child.relative_to(root).as_posix(), "truncated": True})
            continue
        node["children"].append(directory_tree(child, root, depth - 1, budget))
    return node


def workspace_snapshot_id(root: Path, execution_id: str, mode: str) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=1.0,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        head = "unknown"
    payload = {"root": root.as_posix(), "execution_id": execution_id, "head": head, "mode": mode}
    return f"snapshot-{sha256_text(json.dumps(payload, sort_keys=True, separators=(',', ':')))[:24]}"


def copy_workspace_snapshot(root: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            if name in SOURCE_SKIP_DIRS or name.endswith((".o", ".so", ".a", ".pyc")):
                ignored.add(name)
        return ignored

    shutil.copytree(root, destination, ignore=ignore, symlinks=True)


def sandbox_policy(command: str, requested_tier: str | None) -> dict[str, Any]:
    normalized = (requested_tier or "").strip().lower().replace("-", "_")
    command_lower = command.lower()
    if normalized in {"0", "tier_0", "read_only", "readonly"}:
        tier = "tier_0_read_only"
    elif normalized in {"2", "tier_2", "dependency", "dependency_install"}:
        tier = "tier_2_dependency_install"
    elif normalized in {"3", "tier_3", "network"}:
        tier = "tier_3_network"
    elif normalized in {"4", "tier_4", "human", "human_approval", "production"}:
        tier = "tier_4_human_approval"
    elif DEPENDENCY_INSTALL_RE.search(command_lower):
        tier = "tier_2_dependency_install"
    elif NETWORK_COMMAND_RE.search(command_lower):
        tier = "tier_3_network"
    else:
        tier = "tier_1_validation"
    return {
        "tier": tier,
        "network_disabled": tier not in {"tier_2_dependency_install", "tier_3_network"},
        "workspace_mount_mode": "ro" if tier == "tier_0_read_only" else "rw",
        "ephemeral_snapshot": True,
        "requires_human_approval": tier == "tier_4_human_approval",
        "risk_reasons": sandbox_risk_reasons(command_lower, tier),
    }


def sandbox_risk_reasons(command_lower: str, tier: str) -> list[str]:
    reasons: list[str] = []
    if NETWORK_COMMAND_RE.search(command_lower):
        reasons.append("network_command")
    if DEPENDENCY_INSTALL_RE.search(command_lower):
        reasons.append("dependency_install")
    if any(token in command_lower for token in ("rm -rf", "docker run", "kubectl", "terraform apply", "systemctl")):
        reasons.append("destructive_or_host_control_command")
    if tier == "tier_4_human_approval":
        reasons.append("human_approval_tier")
    return reasons


def source_file_allowed(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if any(part in SOURCE_SKIP_DIRS for part in relative.parts):
        return False
    return path.suffix.lower() in SOURCE_EXTENSIONS


def read_bounded_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def source_range_text(lines: list[str], start_line: int, end_line: int) -> str:
    if start_line < 1 or end_line < start_line or not lines:
        return ""
    selected = lines[start_line - 1 : min(end_line, len(lines))]
    return "\n".join(selected)


def attach_candidate_proof(candidate: dict[str, Any], lines: list[str]) -> dict[str, Any]:
    edit_range = candidate.get("edit_range")
    if not isinstance(edit_range, dict):
        return candidate
    start_line = int(edit_range.get("start_line") or candidate.get("line") or 1)
    end_line = int(edit_range.get("end_line") or start_line)
    range_sha256 = sha256_text(source_range_text(lines, start_line, end_line))
    proof_payload = {
        "path": candidate.get("path"),
        "line": candidate.get("line"),
        "symbol": candidate.get("symbol"),
        "edit_range": edit_range,
        "range_sha256": range_sha256,
    }
    candidate["range_sha256"] = range_sha256
    candidate["evidence_id"] = sha256_text(json.dumps(proof_payload, sort_keys=True, separators=(",", ":")))[:24]
    return candidate


def numbered_lines(lines: list[str], first_line: int) -> str:
    return "\n".join(f"{line_number:>6}: {line}" for line_number, line in enumerate(lines, start=first_line))


def iter_source_files(target: Path, root: Path, max_files: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    if target.is_file():
        if source_file_allowed(target, root):
            files.append(target)
        return files, False
    for current, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(name for name in dirnames if name not in SOURCE_SKIP_DIRS)
        for filename in sorted(filenames):
            candidate = Path(current) / filename
            if not candidate.is_file() or not source_file_allowed(candidate, root):
                continue
            if len(files) >= max_files:
                truncated = True
                break
            files.append(candidate)
        if truncated:
            break
    return files, truncated


def source_slice_payload(root: Path, target: Path, start_line: int, end_line: int) -> dict[str, Any]:
    if start_line < 1 or end_line < start_line:
        raise ValueError("line range must be 1-indexed and inclusive")
    if end_line - start_line + 1 > MAX_SOURCE_SLICE_LINES:
        end_line = start_line + MAX_SOURCE_SLICE_LINES - 1
    text = read_bounded_text(target)
    if text is None:
        raise ValueError(f"file is not a bounded UTF-8 source file: {target.relative_to(root).as_posix()}")
    lines = text.splitlines()
    line_count = len(lines)
    if line_count == 0:
        return {
            "ok": True,
            "path": target.relative_to(root).as_posix(),
            "start_line": 1,
            "end_line": 0,
            "numbered_text": "",
        }
    if start_line > line_count:
        raise ValueError(f"start_line {start_line} exceeds file length {line_count}")
    actual_end = min(end_line, line_count)
    selected = lines[start_line - 1 : actual_end]
    proof_candidate = attach_candidate_proof(
        {
            "path": target.relative_to(root).as_posix(),
            "line": start_line,
            "symbol": symbol_near_line(lines, start_line),
            "edit_range": {"start_line": start_line, "end_line": actual_end, "kind": "source_slice"},
        },
        lines,
    )
    return {
        "ok": True,
        "path": target.relative_to(root).as_posix(),
        "start_line": start_line,
        "end_line": actual_end,
        "evidence_id": proof_candidate.get("evidence_id"),
        "range_sha256": proof_candidate.get("range_sha256"),
        "numbered_text": numbered_lines(selected, start_line),
    }


def search_source_payload(
    root: Path,
    target: Path,
    query: str,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    context_lines = max(0, min(context_lines, 5))
    max_results = max(1, min(max_results, MAX_SOURCE_SEARCH_RESULTS))
    files, scan_truncated = iter_source_files(target, root, MAX_SOURCE_SEARCH_FILES)
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    for path in files:
        text = read_bounded_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            haystack = line if case_sensitive else line.lower()
            column = haystack.find(needle)
            if column < 0:
                continue
            snippet_start = max(1, index + 1 - context_lines)
            snippet_end = min(len(lines), index + 1 + context_lines)
            symbol, edit_start_line, edit_end_line, range_kind = symbol_range_near_line(lines, index + 1)
            matches.append(
                attach_candidate_proof(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "line": index + 1,
                        "symbol": symbol,
                        "edit_range": {
                            "start_line": edit_start_line,
                            "end_line": edit_end_line,
                            "kind": range_kind,
                        },
                        "numbered_text": numbered_lines(lines[snippet_start - 1 : snippet_end], snippet_start),
                    },
                    lines,
                )
            )
            if len(matches) >= max_results:
                return {
                    "ok": True,
                    "truncated": True,
                    "matches": matches,
                }
    return {
        "ok": True,
        "truncated": scan_truncated,
        "matches": matches,
    }


def intent_terms(text: str) -> list[str]:
    terms: list[str] = []
    for token in INTENT_WORD_RE.findall(text):
        camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token)
        for part in camel_split.replace("_", " ").replace("-", " ").lower().split():
            if len(part) < 3 or part in INTENT_STOPWORDS:
                continue
            terms.append(part)
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped[:16]


def leading_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def symbol_name_for_line(line: str) -> str | None:
    if line.lstrip().startswith(CONTROL_FLOW_PREFIXES):
        return None
    match = SYMBOL_DEFINITION_RE.match(line)
    if match:
        return match.group(1).split("::")[-1]
    match = CPP_FUNCTION_RE.match(line)
    if match:
        return match.group(1).split("::")[-1]
    return None


def symbol_near_line(lines: list[str], line_number: int) -> str | None:
    search_start = max(0, min(line_number - 1, len(lines) - 1))
    for index in range(search_start, -1, -1):
        symbol = symbol_name_for_line(lines[index])
        if symbol:
            return symbol
    return None


def symbol_range_near_line(lines: list[str], line_number: int) -> tuple[str | None, int, int, str]:
    if not lines:
        return None, 1, 0, "empty"
    clamped_line = max(1, min(line_number, len(lines)))
    search_start = clamped_line - 1
    definition_index = -1
    symbol: str | None = None
    for index in range(search_start, -1, -1):
        symbol = symbol_name_for_line(lines[index])
        if symbol:
            definition_index = index
            break
    if definition_index < 0:
        return None, clamped_line, clamped_line, "line"

    definition = lines[definition_index]
    definition_indent = leading_indent(definition)
    brace_balance = definition.count("{") - definition.count("}")
    last_index = definition_index
    max_end = min(len(lines), definition_index + 120)
    for index in range(definition_index + 1, max_end):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        current_indent = leading_indent(line)
        if brace_balance > 0:
            brace_balance += line.count("{") - line.count("}")
            last_index = index
            if brace_balance <= 0:
                break
            continue
        if symbol_name_for_line(line) and current_indent <= definition_indent:
            break
        if definition.rstrip().endswith(":") and current_indent <= definition_indent and not stripped.startswith(("#", "//")):
            break
        last_index = index
    return symbol, definition_index + 1, last_index + 1, "symbol_block"


def intent_line_window(lines: list[str], line_number: int) -> str:
    if not lines:
        return ""
    start_line = max(1, line_number - INTENT_CONTEXT_LINES)
    end_line = min(len(lines), line_number + INTENT_CONTEXT_LINES)
    return numbered_lines(lines[start_line - 1 : end_line], start_line)


def git_heat_scores(root: Path, limit: int = 240) -> dict[str, tuple[int, int]]:
    try:
        completed = subprocess.run(
            ["git", "log", f"-n{limit}", "--name-only", "--format=__GRAPHKV_TS__%ct"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    heat: dict[str, tuple[int, int]] = {}
    commit_timestamp = 0
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("__GRAPHKV_TS__"):
            try:
                commit_timestamp = int(line.removeprefix("__GRAPHKV_TS__"))
            except ValueError:
                commit_timestamp = 0
            continue
        latest_timestamp, touches = heat.get(line, (0, 0))
        heat[line] = (max(latest_timestamp, commit_timestamp), touches + 1)
    return heat


def stack_trace_targets(root: Path, text: str, max_results: int) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for match in STACK_FRAME_RE.finditer(text):
        raw_path = match.group("path")
        line_number = int(match.group("line"))
        requested = Path(raw_path)
        candidate = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if (relative, line_number) in seen or not candidate.is_file() or not source_file_allowed(candidate, root):
            continue
        source_text = read_bounded_text(candidate)
        if source_text is None:
            continue
        lines = source_text.splitlines()
        if line_number < 1 or line_number > max(1, len(lines)):
            continue
        symbol, edit_start_line, edit_end_line, range_kind = symbol_range_near_line(lines, line_number)
        seen.add((relative, line_number))
        targets.append(
            attach_candidate_proof(
                {
                    "path": relative,
                    "line": line_number,
                    "symbol": symbol,
                    "edit_range": {
                        "start_line": edit_start_line,
                        "end_line": edit_end_line,
                        "kind": range_kind,
                    },
                    "score": 100.0,
                    "confidence": "trace",
                    "reasons": ["execution_trace_graft"],
                    "numbered_text": intent_line_window(lines, line_number),
                },
                lines,
            )
        )
        if len(targets) >= max_results:
            break
    return targets


def failure_memory_path(root: Path) -> Path:
    return root / FAILURE_MEMORY_DIR / FAILURE_MEMORY_FILE


def compact_result_text(result: dict[str, Any], max_chars: int = 6000) -> str:
    chunks: list[str] = []
    for stream_name in ("stderr", "stdout"):
        stream = result.get(stream_name)
        if isinstance(stream, dict):
            text = stream.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
        elif isinstance(stream, str) and stream:
            chunks.append(stream)
    return "\n".join(chunks)[:max_chars]


def error_excerpt(text: str, max_lines: int = 12) -> str:
    selected: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("error", "exception", "failed", "failure", "panic", "traceback")):
            selected.append(line[:240])
        if len(selected) >= max_lines:
            break
    if not selected:
        selected = [line[:240] for line in text.splitlines()[:max_lines]]
    return "\n".join(selected)


def source_file_reference(root: Path, raw_path: str) -> str | None:
    requested = Path(raw_path)
    candidate = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return None
    if not candidate.is_file() or not source_file_allowed(candidate, root):
        return None
    return relative


def compiler_diagnostics(root: Path, text: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for match in COMPILER_DIAGNOSTIC_RE.finditer(text):
        relative = source_file_reference(root, match.group("path"))
        if relative is None:
            continue
        line = int(match.group("line"))
        message = match.group("message").strip()[:300]
        key = (relative, line, message)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            {
                "path": relative,
                "line": line,
                "column": int(match.group("column")) if match.group("column") else None,
                "severity": match.group("severity").lower(),
                "message": message,
            }
        )
        if len(diagnostics) >= 24:
            break
    return diagnostics


def pytest_failures(root: Path, text: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for match in PYTEST_FAILURE_RE.finditer(text):
        relative = source_file_reference(root, match.group("path"))
        if relative is None:
            continue
        symbol = match.group("symbol")
        key = (relative, symbol)
        if key in seen:
            continue
        seen.add(key)
        failures.append({"path": relative, "symbol": symbol, "kind": "pytest_failure"})
        if len(failures) >= 24:
            break
    return failures


def mentioned_source_files(root: Path, text: str) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9_.\-/]+\.(?:c|cc|cpp|cs|go|h|hpp|java|js|jsx|kt|lua|php|py|rb|rs|sh|swift|ts|tsx|vue)", text):
        relative = source_file_reference(root, match.group(0))
        if relative is None or relative in seen:
            continue
        seen.add(relative)
        files.append(relative)
        if len(files) >= 40:
            break
    return files


def symbol_mentions(text: str, trace_targets: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for target in trace_targets:
        symbol = target.get("symbol")
        if isinstance(symbol, str) and symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    for pattern in (
        r"\b([A-Z][A-Za-z0-9_]+(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b",
        r"\b([A-Za-z_][A-Za-z0-9_]*(?:::|\.)(?:[A-Za-z_][A-Za-z0-9_]*))\b",
        r"\b(?:undefined reference to|NameError: name|AttributeError:).*?['`\"]([A-Za-z_][A-Za-z0-9_:]*)['`\"]",
    ):
        for match in re.finditer(pattern, text):
            symbol = match.group(1)
            if symbol in seen or len(symbol) < 3:
                continue
            seen.add(symbol)
            symbols.append(symbol[:160])
            if len(symbols) >= 32:
                return symbols
    for diagnostic in diagnostics:
        for token in INTENT_WORD_RE.findall(str(diagnostic.get("message") or "")):
            if token in seen or len(token) < 4 or token.lower() in INTENT_STOPWORDS:
                continue
            seen.add(token)
            symbols.append(token[:160])
            if len(symbols) >= 32:
                return symbols
    return symbols


def execution_failure_signature(command: str, result: dict[str, Any], text: str, targets: list[dict[str, Any]]) -> str:
    payload = {
        "command": command[:300],
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "excerpt": error_excerpt(text, 8),
        "targets": [(target.get("path"), target.get("line"), target.get("symbol")) for target in targets[:8]],
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))[:24]


def execution_log_document(result: dict[str, Any], evidence: dict[str, Any], text: str) -> dict[str, Any]:
    execution_id = str(result.get("execution_id") or "execution")
    heading = f"{result.get('execution_tier', 'tier_unknown')} exit={result.get('exit_code')} command={result.get('command', '')[:120]}"
    body = "\n".join(
        [
            "# GraphKV Execution Session",
            f"execution_id: {execution_id}",
            f"snapshot_id: {result.get('snapshot_id')}",
            f"patch_id: {result.get('patch_id')}",
            f"tier: {result.get('execution_tier')}",
            f"exit_code: {result.get('exit_code')}",
            f"timed_out: {result.get('timed_out')}",
            f"duration_ms: {result.get('duration_ms')}",
            f"failure_signature: {evidence.get('failure_signature')}",
            f"files: {', '.join(evidence.get('files', []))}",
            f"symbols: {', '.join(evidence.get('symbols', []))}",
            "\n## Error Excerpt",
            str(evidence.get("error_excerpt") or ""),
            "\n## Raw Logs",
            text[:MAX_EXECUTION_EVIDENCE_TEXT],
        ]
    )
    return {
        "document_id": execution_id,
        "chunk_id": f"{execution_id}:logs",
        "path": f"execution/{execution_id}.log",
        "heading": heading,
        "language": "log",
        "type": "execution_log",
        "text": body,
    }


def extract_execution_evidence(root: Path, command: str, result: dict[str, Any]) -> dict[str, Any]:
    text = compact_result_text(result, MAX_EXECUTION_EVIDENCE_TEXT)
    trace_targets = compact_evidence_targets(stack_trace_targets(root, text, 12))
    diagnostics = compiler_diagnostics(root, text)
    test_failures = pytest_failures(root, text)
    files = list(
        dict.fromkeys(
            [target["path"] for target in trace_targets if isinstance(target.get("path"), str)]
            + [diagnostic["path"] for diagnostic in diagnostics if isinstance(diagnostic.get("path"), str)]
            + [failure["path"] for failure in test_failures if isinstance(failure.get("path"), str)]
            + mentioned_source_files(root, text)
        )
    )[:40]
    symbols = symbol_mentions(text, trace_targets, diagnostics)
    signature = execution_failure_signature(command, result, text, trace_targets) if not result.get("ok") else None
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "evidence_contract": "execution_derived_retrieval_signal",
        "files": files,
        "symbols": symbols,
        "stack_traces": trace_targets,
        "compiler_diagnostics": diagnostics,
        "test_failures": test_failures,
        "error_excerpt": error_excerpt(text),
        "failure_signature": signature,
        "confidence_delta": 0.25 if result.get("ok") else -0.4,
        "graph_updates": {
            "execution_node": result.get("execution_id"),
            "patch_node": result.get("patch_id"),
            "edges": execution_graph_edges(result, files, symbols, signature),
        },
    }
    evidence["log_document"] = execution_log_document(result, evidence, text)
    return evidence


def execution_graph_edges(result: dict[str, Any], files: list[str], symbols: list[str], signature: str | None) -> list[dict[str, Any]]:
    execution_id = result.get("execution_id")
    edges: list[dict[str, Any]] = []
    if result.get("patch_id"):
        edges.append({"from": result.get("patch_id"), "to": execution_id, "type": "validated_by"})
    for path in files[:12]:
        edges.append({"from": execution_id, "to": path, "type": "implicated_file"})
    for symbol in symbols[:12]:
        edges.append({"from": execution_id, "to": symbol, "type": "implicated_symbol"})
    if signature:
        edges.append({"from": execution_id, "to": signature, "type": "observed_failure_signature"})
    return edges


def compact_evidence_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for target in targets:
        compacted.append(
            {
                "path": target.get("path"),
                "line": target.get("line"),
                "symbol": target.get("symbol"),
                "edit_range": target.get("edit_range"),
                "evidence_id": target.get("evidence_id"),
                "range_sha256": target.get("range_sha256"),
                "confidence": target.get("confidence"),
                "reasons": target.get("reasons", []),
            }
        )
    return compacted


def load_failure_memory(root: Path, max_records: int = MAX_FAILURE_MEMORY_RECORDS) -> list[dict[str, Any]]:
    path = failure_memory_path(root)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_records:]
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def record_failure_memory(root: Path, command: str, result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok"):
        return None
    text = compact_result_text(result)
    excerpt = error_excerpt(text)
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    evidence_targets = evidence.get("stack_traces") if isinstance(evidence, dict) else None
    targets = list(evidence_targets or compact_evidence_targets(stack_trace_targets(root, text, 4)))[:8]
    signature_payload = {
        "command": command,
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "excerpt": excerpt,
        "targets": [(target.get("path"), target.get("line")) for target in targets],
    }
    signature = str(evidence.get("failure_signature") or "") if isinstance(evidence, dict) else ""
    if not signature:
        signature = sha256_text(json.dumps(signature_payload, sort_keys=True, separators=(",", ":")))[:24]
    record = {
        "schema_version": 1,
        "kind": "sandbox_failure",
        "signature": signature,
        "observed_at": datetime.now(UTC).isoformat(),
        "execution_id": result.get("execution_id"),
        "snapshot_id": result.get("snapshot_id"),
        "patch_id": result.get("patch_id"),
        "execution_tier": result.get("execution_tier"),
        "command": command[:500],
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "error_excerpt": excerpt,
        "evidence_targets": targets,
        "files": evidence.get("files", []) if isinstance(evidence, dict) else [],
        "symbols": evidence.get("symbols", []) if isinstance(evidence, dict) else [],
    }
    path = failure_memory_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        return None
    return {"recorded": True, "signature": signature, "path": path.relative_to(root).as_posix()}


def match_failure_memory(
    root: Path,
    query: str,
    observed_error: str | None,
    max_hits: int = MAX_FAILURE_MEMORY_HITS,
) -> list[dict[str, Any]]:
    terms = set(intent_terms("\n".join(part for part in (query, observed_error or "") if part)))
    if not terms and not observed_error:
        return []
    hits: list[dict[str, Any]] = []
    for record in reversed(load_failure_memory(root)):
        haystack_parts = [
            str(record.get("command") or ""),
            str(record.get("error_excerpt") or ""),
        ]
        for target in record.get("evidence_targets") or []:
            if isinstance(target, dict):
                haystack_parts.extend(str(target.get(field) or "") for field in ("path", "symbol"))
        haystack = "\n".join(haystack_parts).lower()
        term_hits = sum(1 for term in terms if term in haystack)
        target_bonus = 3 if any(
            isinstance(target, dict) and str(target.get("path") or "").lower() in (observed_error or "").lower()
            for target in record.get("evidence_targets") or []
        ) else 0
        score = term_hits + target_bonus
        if score <= 0:
            continue
        hits.append(
            {
                "signature": record.get("signature"),
                "score": score,
                "observed_at": record.get("observed_at"),
                "command": record.get("command"),
                "exit_code": record.get("exit_code"),
                "timed_out": record.get("timed_out"),
                "error_excerpt": record.get("error_excerpt"),
                "evidence_targets": record.get("evidence_targets") or [],
            }
        )
        if len(hits) >= max_hits:
            break
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:max_hits]


def failure_path_scores(hits: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for hit in hits:
        for target in hit.get("evidence_targets") or []:
            if not isinstance(target, dict):
                continue
            path = target.get("path")
            if not isinstance(path, str) or not path:
                continue
            scores[path] = scores.get(path, 0.0) + min(10.0, float(hit.get("score") or 1) * 2.0)
    return scores


def config_dependencies_for_path(root: Path, relative_path: str) -> list[str]:
    dependencies: list[str] = []
    try:
        source_path = resolve_workspace_path(root, relative_path)
    except ValueError:
        return dependencies
    current = source_path.parent if source_path.suffix else source_path
    while True:
        try:
            current.relative_to(root)
        except ValueError:
            break
        for filename in CONFIG_DEPENDENCY_FILES:
            candidate = current / filename
            if candidate.is_file():
                dependencies.append(candidate.relative_to(root).as_posix())
        if current == root:
            break
        current = current.parent
    deduped: list[str] = []
    seen: set[str] = set()
    for dependency in dependencies:
        if dependency in seen:
            continue
        seen.add(dependency)
        deduped.append(dependency)
    return deduped[:6]


def likely_test_paths(
    root: Path,
    relative_path: str,
    symbol: str | None,
    terms: list[str],
    cached_files: list[tuple[Path, str, list[str], dict[str, int]]] | None,
    max_tests: int = 3,
) -> list[str]:
    candidate_terms = set(terms[:6])
    path_stem = Path(relative_path).stem.lower()
    candidate_terms.update(part for part in re.split(r"[^a-z0-9]+", path_stem) if len(part) >= 3)
    if symbol:
        candidate_terms.update(intent_terms(symbol))
        candidate_terms.add(symbol.lower())
    if not candidate_terms:
        return []
    if cached_files is None:
        files, _truncated = iter_source_files(root, root, min(400, MAX_INTENT_ROUTER_FILES))
        file_rows = [(path, "", [], {}) for path in files]
    else:
        file_rows = cached_files
    scored: list[tuple[int, str]] = []
    for path, _source_text, _lines, _counts in file_rows:
        relative = path.relative_to(root).as_posix()
        lower = relative.lower()
        if "test" not in lower and "spec" not in lower:
            continue
        score = sum(1 for term in candidate_terms if term in lower)
        if score <= 0:
            continue
        scored.append((score, relative))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [relative for _score, relative in scored[:max_tests]]


def candidate_span_lines(candidate: dict[str, Any]) -> int:
    edit_range = candidate.get("edit_range")
    if not isinstance(edit_range, dict):
        return 1
    start_line = int(edit_range.get("start_line") or candidate.get("line") or 1)
    end_line = int(edit_range.get("end_line") or start_line)
    return max(0, end_line - start_line + 1)


def attach_causal_chain(
    root: Path,
    candidate: dict[str, Any],
    terms: list[str],
    cached_files: list[tuple[Path, str, list[str], dict[str, int]]] | None,
    failure_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    path = str(candidate.get("path") or "")
    symbol = candidate.get("symbol") if isinstance(candidate.get("symbol"), str) else None
    matching_failures = [
        hit.get("signature")
        for hit in failure_hits
        for target in hit.get("evidence_targets") or []
        if isinstance(target, dict) and target.get("path") == path
    ]
    candidate["causal_chain"] = {
        "primary_span": {
            "path": path,
            "line": candidate.get("line"),
            "symbol": symbol,
            "edit_range": candidate.get("edit_range"),
            "evidence_id": candidate.get("evidence_id"),
            "range_sha256": candidate.get("range_sha256"),
        },
        "signals": candidate.get("reasons", []),
        "config_dependencies": config_dependencies_for_path(root, path),
        "likely_tests": likely_test_paths(root, path, symbol, terms, cached_files),
        "failure_memory_signatures": list(dict.fromkeys(signature for signature in matching_failures if signature))[:3],
        "runtime_trace": "execution_trace_graft" in candidate.get("reasons", []),
    }
    return candidate


def confidence_score(targets: list[dict[str, Any]]) -> float:
    if not targets:
        return 0.0
    top = targets[0]
    if top.get("confidence") == "trace":
        return 0.95
    score = float(top.get("score") or 0.0)
    if score >= 40:
        base = 0.9
    elif score >= 24:
        base = 0.78
    elif score >= 10:
        base = 0.58
    else:
        base = 0.34
    if len(targets) > 1:
        runner_up = float(targets[1].get("score") or 0.0)
        if runner_up and score and runner_up / score >= 0.85:
            base -= 0.12
    return round(max(0.0, min(0.99, base)), 2)


def risk_level(query: str, targets: list[dict[str, Any]], confidence: float) -> str:
    broad_markers = {"architecture", "refactor", "rename", "security", "concurrency", "auth", "database", "storage"}
    terms = set(intent_terms(query))
    if not targets or confidence < 0.45 or broad_markers.intersection(terms):
        return "high"
    if confidence < 0.7 or candidate_span_lines(targets[0]) > 80:
        return "medium"
    return "low"


def validation_strategy_for_targets(targets: list[dict[str, Any]]) -> dict[str, Any]:
    commands: list[str] = []
    test_paths: list[str] = []
    for target in targets[:3]:
        path = str(target.get("path") or "")
        if not path:
            continue
        causal_chain = target.get("causal_chain") if isinstance(target.get("causal_chain"), dict) else {}
        for test_path in causal_chain.get("likely_tests", []) if isinstance(causal_chain, dict) else []:
            if test_path not in test_paths:
                test_paths.append(test_path)
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            commands.append(f"python3 -m py_compile {shlex.quote(path)}")
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            commands.append("npm test -- --runInBand")
        elif suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}:
            commands.append("cmake --build build")
        elif suffix == ".rs":
            commands.append("cargo test")
    for test_path in test_paths[:2]:
        if test_path.endswith(".py"):
            commands.insert(0, f"python3 -m pytest {shlex.quote(test_path)}")
    deduped_commands = list(dict.fromkeys(commands))[:4]
    return {
        "primary": "validate the edited span and its nearest tests, then inspect git diff",
        "suggested_commands": deduped_commands,
        "fallback": "run the smallest build/test command that covers the top causal dependency",
    }


def likely_subsystem_from_targets(targets: list[dict[str, Any]]) -> str | None:
    if not targets:
        return None
    path = str(targets[0].get("path") or "")
    if not path:
        return None
    parts = Path(path).parts
    if len(parts) <= 1:
        return "."
    return parts[0]


def structured_task_payload(query: str, observed_error: str | None, targets: list[dict[str, Any]]) -> dict[str, Any]:
    confidence = confidence_score(targets)
    risk = risk_level(query, targets, confidence)
    probable_files = [
        {
            "path": target.get("path"),
            "symbol": target.get("symbol"),
            "edit_range": target.get("edit_range"),
            "confidence": target.get("confidence"),
            "evidence_id": target.get("evidence_id"),
        }
        for target in targets[:5]
    ]
    return {
        "objective": " ".join(query.strip().split())[:240],
        "likely_subsystem": likely_subsystem_from_targets(targets),
        "probable_files": probable_files,
        "confidence": confidence,
        "risk_level": risk,
        "runtime_signal_present": bool(observed_error and observed_error.strip()) or any(
            "execution_trace_graft" in target.get("reasons", []) for target in targets
        ),
        "validation_strategy": validation_strategy_for_targets(targets),
        "rollback_strategy": "use inspect_git_state(diff, target_file) and restore the exact edit_source_code proof range if validation fails",
    }


def edit_beam_payload(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    beam: list[dict[str, Any]] = []
    for index, target in enumerate(targets[:4], start=1):
        beam.append(
            {
                "hypothesis_id": f"H{index}",
                "path": target.get("path"),
                "symbol": target.get("symbol"),
                "edit_range": target.get("edit_range"),
                "evidence_id": target.get("evidence_id"),
                "range_sha256": target.get("range_sha256"),
                "score": target.get("score"),
                "confidence": target.get("confidence"),
                "why": target.get("reasons", []),
                "span_lines": candidate_span_lines(target),
            }
        )
    return beam


def adversarial_review_payload(targets: list[dict[str, Any]], task: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not targets:
        findings.append({"severity": "block", "issue": "no ranked edit target", "action": "narrow intent or provide runtime evidence"})
    else:
        top = targets[0]
        if candidate_span_lines(top) > 80:
            findings.append({"severity": "warn", "issue": "top edit span is broad", "action": "search_source for a narrower anchor before editing"})
        if task.get("confidence", 0.0) < 0.65:
            findings.append({"severity": "warn", "issue": "low target confidence", "action": "prefer execution-guided evidence or a more specific source search"})
        if len(targets) > 1:
            top_score = float(top.get("score") or 0.0)
            second_score = float(targets[1].get("score") or 0.0)
            if top_score and second_score / top_score >= 0.85:
                findings.append(
                    {
                        "severity": "warn",
                        "issue": "close competing edit target",
                        "challenger": {
                            "path": targets[1].get("path"),
                            "symbol": targets[1].get("symbol"),
                            "edit_range": targets[1].get("edit_range"),
                            "evidence_id": targets[1].get("evidence_id"),
                        },
                        "action": "compare the challenger evidence before committing a broad change",
                    }
                )
        validation = task.get("validation_strategy") if isinstance(task.get("validation_strategy"), dict) else {}
        if not validation.get("suggested_commands"):
            findings.append({"severity": "warn", "issue": "no deterministic validation command inferred", "action": "choose the smallest local build/test command after editing"})
    verdict = "block" if any(finding.get("severity") == "block" for finding in findings) else "proceed_with_caution" if findings else "proceed"
    return {"verdict": verdict, "findings": findings}


def rank_response_payload(
    root: Path,
    query: str,
    observed_error: str | None,
    terms: list[str],
    truncated: bool,
    targets: list[dict[str, Any]],
    intent_contract: str,
    cached_files: list[tuple[Path, str, list[str], dict[str, int]]] | None,
    failure_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    annotated_targets = [attach_causal_chain(root, target, terms, cached_files, failure_hits) for target in targets]
    structured_task = structured_task_payload(query, observed_error, annotated_targets)
    edit_beam = edit_beam_payload(annotated_targets)
    adversarial_review = adversarial_review_payload(annotated_targets, structured_task)
    return {
        "ok": True,
        "intent_contract": intent_contract,
        "terms": terms,
        "truncated": truncated,
        "structured_task": structured_task,
        "edit_beam": edit_beam,
        "adversarial_review": adversarial_review,
        "failure_memory_hits": failure_hits,
        "observability": {
            "ranking_trace_id": sha256_text(
                json.dumps(
                    {
                        "query": query,
                        "observed_error": observed_error,
                        "target_ids": [target.get("evidence_id") for target in annotated_targets],
                        "failure_signatures": [hit.get("signature") for hit in failure_hits],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )[:24],
            "beam_width": len(edit_beam),
            "top_confidence": structured_task.get("confidence"),
            "risk_level": structured_task.get("risk_level"),
        },
        "targets": annotated_targets,
    }


def rank_intent_targets_payload(
    root: Path,
    target: Path,
    query: str,
    observed_error: str | None,
    max_results: int,
) -> dict[str, Any]:
    combined_query = "\n".join(part for part in (query.strip(), (observed_error or "").strip()) if part)
    terms = intent_terms(combined_query)
    if not terms and not observed_error:
        raise ValueError("query must contain at least one non-generic intent term")
    max_results = max(1, min(max_results, MAX_INTENT_ROUTER_RESULTS))
    failure_hits = match_failure_memory(root, query, observed_error, MAX_FAILURE_MEMORY_HITS)
    remembered_path_scores = failure_path_scores(failure_hits)
    trace_targets = stack_trace_targets(root, combined_query, max_results)
    if trace_targets and (observed_error or any(term in TRACE_FAST_TERMS for term in terms)):
        for candidate in trace_targets:
            candidate["next_step"] = "Call query_context with target_node=path, then search_source on the returned symbol/anchor, then edit_source_code immediately."
        return rank_response_payload(
            root,
            query,
            observed_error,
            terms,
            False,
            trace_targets,
            "execution_trace_fast_path_before_zero_hesitation_edit",
            None,
            failure_hits,
        )
    files, scan_truncated = iter_source_files(target, root, MAX_INTENT_ROUTER_FILES)
    cached_files: list[tuple[Path, str, list[str], dict[str, int]]] = []
    document_frequency = {term: 0 for term in terms}
    for path in files:
        source_text = read_bounded_text(path)
        if source_text is None:
            continue
        relative = path.relative_to(root).as_posix()
        lower_blob = f"{relative}\n{source_text}".lower()
        term_counts = {term: lower_blob.count(term) for term in terms}
        for term, count in term_counts.items():
            if count > 0:
                document_frequency[term] += 1
        cached_files.append((path, source_text, source_text.splitlines(), term_counts))

    file_count = max(1, len(cached_files))
    inverse_document_frequency = {
        term: 1.0 + math.log((file_count + 1) / (document_frequency[term] + 1)) for term in terms
    }
    now_timestamp = int(datetime.now(UTC).timestamp())
    git_heat = git_heat_scores(root)
    preliminary: list[dict[str, Any]] = []

    for path, source_text, lines, term_counts in cached_files:
        relative = path.relative_to(root).as_posix()
        lower_path = relative.lower()
        lexical_score = sum(min(count, 12) * inverse_document_frequency[term] for term, count in term_counts.items())
        path_score = sum(4.0 * inverse_document_frequency[term] for term in terms if term in lower_path)
        if lexical_score <= 0 and path_score <= 0:
            continue

        best_line = 1
        best_line_score = path_score
        best_symbol: str | None = None
        for line_index, line in enumerate(lines, start=1):
            lower_line = line.lower()
            line_score = sum((lower_line.count(term) * 3.0) * inverse_document_frequency[term] for term in terms)
            symbol_name = symbol_name_for_line(line)
            if symbol_name:
                lower_symbol = symbol_name.lower()
                if any(term in lower_symbol for term in terms):
                    line_score += 8.0
            if line_score > best_line_score:
                best_line = line_index
                best_line_score = line_score
                best_symbol = symbol_name

        symbol, edit_start_line, edit_end_line, range_kind = symbol_range_near_line(lines, best_line)
        symbol = best_symbol or symbol
        complexity_hits = sum(1 for line in lines if COMPLEXITY_RE.search(line))
        entropy_score = min(4.0, complexity_hits / 8.0)
        latest_timestamp, touches = git_heat.get(relative, (0, 0))
        age_days = max(0.0, (now_timestamp - latest_timestamp) / 86_400.0) if latest_timestamp else 3650.0
        recency_score = 2.0 / (1.0 + (age_days / 30.0)) if latest_timestamp else 0.0
        touch_score = min(2.0, touches / 4.0)
        lower_name = path.name.lower()
        test_rebound_score = 2.0 if ("test" in lower_path or lower_name.startswith("test_")) and best_line_score > 0 else 0.0
        remembered_failure_score = remembered_path_scores.get(relative, 0.0)
        total_score = (
            lexical_score
            + path_score
            + best_line_score
            + entropy_score
            + recency_score
            + touch_score
            + test_rebound_score
            + remembered_failure_score
        )
        reasons: list[str] = []
        if lexical_score > 0:
            reasons.append("lexical_tfidf")
        if path_score > 0:
            reasons.append("path_fingerprint")
        if entropy_score >= 1.0:
            reasons.append("entropy_sieve")
        if recency_score or touch_score:
            reasons.append("git_heatmap")
        if test_rebound_score:
            reasons.append("semantic_rebound_tests")
        if remembered_failure_score:
            reasons.append("failure_memory")
        if symbol and any(term in symbol.lower() for term in terms):
            reasons.append("symbol_name_match")
        preliminary.append(
            attach_candidate_proof(
                {
                    "path": relative,
                    "line": best_line,
                    "symbol": symbol,
                    "edit_range": {
                        "start_line": edit_start_line,
                        "end_line": edit_end_line,
                        "kind": range_kind,
                    },
                    "score": total_score,
                    "confidence": "high" if total_score >= 24 else "medium" if total_score >= 10 else "low",
                    "reasons": reasons,
                    "numbered_text": intent_line_window(lines, best_line),
                },
                lines,
            )
        )

    preliminary.sort(key=lambda item: item["score"], reverse=True)
    top_candidates = preliminary[: max_results * 4]
    if top_candidates:
        lower_blobs = [source_text.lower() for _path, source_text, _lines, _counts in cached_files]
        for candidate in top_candidates:
            symbol = candidate.get("symbol")
            if not isinstance(symbol, str) or len(symbol) < 3:
                continue
            reference_count = sum(1 for lower_blob in lower_blobs if symbol.lower() in lower_blob)
            if reference_count > 1:
                candidate["score"] = round(float(candidate["score"]) + min(4.0, reference_count / 3.0), 3)
                candidate["reasons"].append("centrality_proxy")

    merged: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, int]] = set()
    for candidate in [*trace_targets, *sorted(top_candidates, key=lambda item: item["score"], reverse=True)]:
        key = (str(candidate["path"]), int(candidate["line"]))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        candidate["score"] = round(float(candidate["score"]), 3)
        candidate["next_step"] = "Call query_context with target_node=path, then search_source on the returned symbol/anchor, then edit_source_code immediately."
        merged.append(candidate)
        if len(merged) >= max_results:
            break

    return rank_response_payload(
        root,
        query,
        observed_error,
        terms,
        scan_truncated,
        merged,
        "vague_intent_collapse_before_zero_hesitation_edit",
        cached_files,
        failure_hits,
    )


def graph_context_status(context_text: str) -> dict[str, Any]:
    def extract_int(name: str) -> int | None:
        match = re.search(rf"^{re.escape(name)}:\s*(\d+)", context_text, flags=re.MULTILINE)
        return int(match.group(1)) if match else None

    selected_nodes = extract_int("selected_nodes")
    selected_edges = extract_int("selected_edges")
    snapshot_unavailable = "snapshot: unavailable" in context_text or "doc_snapshot: unavailable" in context_text
    thin = not context_text.strip() or selected_nodes == 0 or snapshot_unavailable
    return {
        "selected_nodes": selected_nodes,
        "selected_edges": selected_edges,
        "snapshot_unavailable": snapshot_unavailable,
        "thin": thin,
    }


def target_node_path(root: Path, target_node: str | None) -> tuple[Path, str]:
    if not target_node:
        return root, "workspace_root"
    requested = target_node.split(":", 1)[0].strip()
    if not requested:
        return root, "workspace_root"
    try:
        candidate = resolve_workspace_path(root, requested)
    except ValueError:
        return root, "symbol_or_external_target"
    if candidate.exists():
        return candidate, "target_node_path"
    return root, "symbol_or_missing_target"


def file_symbol_candidates(root: Path, target: Path, max_results: int) -> list[dict[str, Any]]:
    source_text = read_bounded_text(target)
    if source_text is None:
        return []
    lines = source_text.splitlines()
    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        symbol = symbol_name_for_line(line)
        if not symbol:
            continue
        _symbol, start_line, end_line, range_kind = symbol_range_near_line(lines, line_number)
        candidates.append(
            attach_candidate_proof(
                {
                    "path": target.relative_to(root).as_posix(),
                    "line": line_number,
                    "symbol": symbol,
                    "edit_range": {"start_line": start_line, "end_line": end_line, "kind": range_kind},
                    "score": 1.0,
                    "confidence": "fallback",
                    "reasons": ["target_file_symbol_index"],
                    "numbered_text": intent_line_window(lines, line_number),
                },
                lines,
            )
        )
        if len(candidates) >= max_results:
            break
    if candidates or not lines:
        return candidates
    return [
        attach_candidate_proof(
            {
                "path": target.relative_to(root).as_posix(),
                "line": 1,
                "symbol": None,
                "edit_range": {"start_line": 1, "end_line": min(1, len(lines)), "kind": "file_start"},
                "score": 0.5,
                "confidence": "fallback",
                "reasons": ["target_file_start"],
                "numbered_text": intent_line_window(lines, 1),
            },
            lines,
        )
    ]


def compact_local_candidates(candidates: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for candidate in candidates[:max_results]:
        compacted.append(
            {
                "path": candidate.get("path"),
                "line": candidate.get("line"),
                "symbol": candidate.get("symbol"),
                "edit_range": candidate.get("edit_range"),
                "evidence_id": candidate.get("evidence_id"),
                "range_sha256": candidate.get("range_sha256"),
                "score": candidate.get("score"),
                "confidence": candidate.get("confidence"),
                "reasons": candidate.get("reasons", []),
                "numbered_text": candidate.get("numbered_text"),
            }
        )
    return compacted


def query_context_local_evidence(
    root: Path,
    query: str,
    target_node: str | None,
    graph_status: dict[str, Any],
) -> dict[str, Any]:
    target, target_basis = target_node_path(root, target_node)
    combined_query = "\n".join(part for part in (query.strip(), (target_node or "").strip()) if part)
    ranker_error: str | None = None
    ranker_contract: str | None = None
    structured_task: dict[str, Any] | None = None
    edit_beam: list[dict[str, Any]] = []
    adversarial_review: dict[str, Any] | None = None
    failure_hits: list[dict[str, Any]] = []
    observability: dict[str, Any] | None = None
    try:
        ranked = rank_intent_targets_payload(root, target, combined_query, None, MAX_QUERY_CONTEXT_CANDIDATES)
        ranker_contract = str(ranked.get("intent_contract") or "")
        candidates = compact_local_candidates(list(ranked.get("targets") or []), MAX_QUERY_CONTEXT_CANDIDATES)
        structured_task = ranked.get("structured_task") if isinstance(ranked.get("structured_task"), dict) else None
        edit_beam = list(ranked.get("edit_beam") or [])[:MAX_QUERY_CONTEXT_CANDIDATES]
        adversarial_review = ranked.get("adversarial_review") if isinstance(ranked.get("adversarial_review"), dict) else None
        failure_hits = list(ranked.get("failure_memory_hits") or [])[:MAX_FAILURE_MEMORY_HITS]
        observability = ranked.get("observability") if isinstance(ranked.get("observability"), dict) else None
    except Exception as error:
        ranker_error = str(error)
        candidates = compact_local_candidates(
            file_symbol_candidates(root, target, MAX_QUERY_CONTEXT_CANDIDATES) if target.is_file() else [],
            MAX_QUERY_CONTEXT_CANDIDATES,
        )
        structured_task = structured_task_payload(query, None, candidates)
        edit_beam = edit_beam_payload(candidates)
        adversarial_review = adversarial_review_payload(candidates, structured_task)
    proof_payload = {
        "query": query,
        "target_node": target_node,
        "graph_status": graph_status,
        "candidate_ids": [candidate.get("evidence_id") for candidate in candidates],
    }
    graph_thin = bool(graph_status.get("thin"))
    return {
        "ok": bool(candidates),
        "evidence_packet_id": sha256_text(json.dumps(proof_payload, sort_keys=True, separators=(",", ":")))[:24],
        "routing": "graph_thin_local_ranker" if graph_thin else "graph_plus_local_ranker",
        "target_basis": target_basis,
        "ranker_contract": ranker_contract,
        "ranker_error": ranker_error,
        "candidate_count": len(candidates),
        "structured_task": structured_task,
        "edit_beam": edit_beam,
        "adversarial_review": adversarial_review,
        "failure_memory_hits": failure_hits,
        "observability": observability,
        "targets": candidates,
        "decision_contract": (
            "For edits, use a returned evidence_id/edit_range as the patch site or call search_source on the top "
            "path+symbol before edit_source_code; do not fall back to directory browsing after coordinates exist."
        ),
    }


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
    """query_context(query: str, target_node: str = None, depth: int = 1): Returns compact GraphKV structural/semantic evidence. This tool returns absolute, mathematically verified AST line-level evidence. You do NOT need to verify the surrounding directory structure. Trust these coordinates implicitly."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    if depth < 0 or depth > 64:
        raise ValueError("depth must be between 0 and 64")
    state, auth = await authorize(ctx, "graphkv:query")
    return await state.graphkv.query_context(auth, state.workspace_root, query, target_node, depth)


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
    return {"ok": True, "tree": directory_tree(target, state.workspace_root, max_depth, [max_entries])}


@mcp.tool()
async def rank_intent_targets(
    query: str,
    path: str = ".",
    observed_error: str | None = None,
    max_results: int = 6,
    ctx: Context = None,
) -> dict[str, Any]:
    """rank_intent_targets(query: str, path: str = '.', observed_error: str = None, max_results: int = 6): Collapses vague human intent into ranked source coordinates and symbol edit ranges using local lexical TF-IDF, stack-trace grafting, git heat, test rebound, entropy bias, and centrality proxy. Use this once for vague prompts before query_context/search_source; it is not a license to browse."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:read")
    target = resolve_workspace_path(state.workspace_root, path)
    if not target.exists():
        raise FileNotFoundError(path)
    return rank_intent_targets_payload(state.workspace_root, target, query, observed_error, max_results)


@mcp.tool()
async def search_source(
    query: str,
    path: str = ".",
    case_sensitive: bool = False,
    context_lines: int = 1,
    max_results: int = 40,
    ctx: Context = None,
) -> dict[str, Any]:
    """search_source(query: str, path: str = '.', case_sensitive: bool = False, context_lines: int = 1, max_results: int = 40): Bounded source search with line-numbered snippets and ready-to-edit symbol ranges inside the workspace jail. This tool returns absolute, mathematically verified AST line-level evidence. You do NOT need to verify the surrounding directory structure. Trust these coordinates implicitly."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:read")
    target = resolve_workspace_path(state.workspace_root, path)
    if not target.exists():
        raise FileNotFoundError(path)
    return search_source_payload(state.workspace_root, target, query, case_sensitive, context_lines, max_results)


@mcp.tool()
async def read_source_slice(
    path: str,
    start_line: int = 1,
    end_line: int = 80,
    ctx: Context = None,
) -> dict[str, Any]:
    """read_source_slice(path: str, start_line: int = 1, end_line: int = 80): Returns bounded line-numbered source coordinates inside the workspace jail. Once these line numbers are available, call edit_source_code immediately."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, _auth = await authorize(ctx, "workspace:read")
    target = resolve_workspace_path(state.workspace_root, path)
    if not target.is_file() or not source_file_allowed(target, state.workspace_root):
        raise FileNotFoundError(path)
    return source_slice_payload(state.workspace_root, target, start_line, end_line)


@mcp.tool()
async def execute_sandbox(
    command: str,
    timeout_seconds: int = 30,
    execution_tier: str | None = None,
    patch_id: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """execute_sandbox(command: str, timeout_seconds: int = 30, execution_tier: str = None, patch_id: str = None): Executes the command in an ephemeral Docker workspace snapshot, extracts execution-derived evidence, records failure memory, and asynchronously sends compact logs to GraphKV/Warden."""
    if ctx is None:
        raise RuntimeError("MCP request context is required")
    state, auth = await authorize(ctx, "workspace:execute")
    result = await state.sandbox.run(command, timeout_seconds, execution_tier, patch_id)
    failure_record = record_failure_memory(state.workspace_root, command, result)
    result["failure_memory"] = failure_record or {"recorded": False}
    task = asyncio.create_task(state.graphkv.ingest_sandbox_logs(auth, command, result))
    task.add_done_callback(observe_background_task)
    return result


@mcp.tool()
async def edit_source_code(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    expected_range_sha256: str | None = None,
    evidence_id: str | None = None,
    reason: str | None = None,
    validation_command: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """edit_source_code(path: str, start_line: int, end_line: int, new_content: str, expected_range_sha256: str = None, evidence_id: str = None, reason: str = None, validation_command: str = None): Reads the file, optionally verifies the expected range hash, replaces the exact line range, and returns a proof record."""
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
    before_range_text = source_range_text(original.splitlines(), start_line, end_line)
    before_range_sha256 = sha256_text(before_range_text)
    if expected_range_sha256 and expected_range_sha256 != before_range_sha256:
        raise ValueError(
            "expected_range_sha256 did not match current file contents; rerun search_source or read_source_slice before editing"
        )
    normalized_new_content = new_content
    range_had_trailing_newline = bool(original_lines[end_line - 1].endswith(("\n", "\r")))
    if normalized_new_content and range_had_trailing_newline and not normalized_new_content.endswith(("\n", "\r")):
        normalized_new_content += "\n"
    replacement = normalized_new_content.splitlines(keepends=True)
    after_range_text = "\n".join(normalized_new_content.splitlines())
    updated_lines = original_lines[: start_line - 1] + replacement + original_lines[end_line:]
    updated = "".join(updated_lines)
    target.write_text(updated, encoding="utf-8")
    before_file_sha256 = sha256_text(original)
    after_file_sha256 = sha256_text(updated)
    after_range_sha256 = sha256_text(after_range_text)
    proof_payload = {
        "path": target.relative_to(state.workspace_root).as_posix(),
        "start_line": start_line,
        "end_line": end_line,
        "new_end_line": start_line + len(replacement) - 1,
        "before_file_sha256": before_file_sha256,
        "after_file_sha256": after_file_sha256,
        "before_range_sha256": before_range_sha256,
        "after_range_sha256": after_range_sha256,
        "evidence_id": evidence_id,
        "reason": reason,
    }
    checker_findings: list[dict[str, Any]] = []
    if not expected_range_sha256:
        checker_findings.append(
            {"severity": "warn", "issue": "edit was applied without an expected range hash", "action": "pass range_sha256 from search_source or query_context local_evidence on future edits"}
        )
    if not evidence_id:
        checker_findings.append(
            {"severity": "warn", "issue": "edit was applied without an evidence_id", "action": "thread evidence_id from the selected ranked/search result"}
        )
    if end_line - start_line + 1 > 80 or len(replacement) > 80:
        checker_findings.append(
            {"severity": "warn", "issue": "large line-range replacement", "action": "validate with targeted tests and inspect_git_state(diff, target_file)"}
        )
    if not validation_command:
        checker_findings.append(
            {"severity": "info", "issue": "no validation command attached", "action": "run the smallest command that covers the edited span"}
        )
    return {
        "ok": True,
        "path": target.relative_to(state.workspace_root).as_posix(),
        "start_line": start_line,
        "end_line": end_line,
        "new_end_line": start_line + len(replacement) - 1,
        "changed": original != updated,
        "proof": {
            "edit_proof_id": sha256_text(json.dumps(proof_payload, sort_keys=True, separators=(",", ":")))[:24],
            "evidence_id": evidence_id,
            "range_hash_verified": expected_range_sha256 == before_range_sha256 if expected_range_sha256 else False,
            "before_file_sha256": before_file_sha256,
            "after_file_sha256": after_file_sha256,
            "before_range_sha256": before_range_sha256,
            "after_range_sha256": after_range_sha256,
            "expected_range_sha256": expected_range_sha256,
            "invariant": reason or "only the requested inclusive line range was replaced",
            "validation_command": validation_command,
        },
        "adversarial_review": {
            "verdict": "review_warnings" if checker_findings else "proof_verified",
            "findings": checker_findings,
        },
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