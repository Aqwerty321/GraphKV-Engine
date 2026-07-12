#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


ARTIFACT_DIR_ENV = "GRAPHKV_ARTIFACT_DIR_NAME"
DEFAULT_ARTIFACT_DIR = "graphkv-out"
TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text) if len(token) > 1]


def _score_blob(blob: str, terms: list[str]) -> float:
    if not terms:
        return 0.0
    lower = blob.lower()
    score = 0.0
    for term in terms:
        if term in lower:
            score += 3.0 if lower == term else 1.0
    return score


def _compact_node(node: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "label", "kind", "path", "line", "symbol", "language", "community", "degree")
    return {key: node[key] for key in keys if key in node and node[key] not in (None, "")}


def _compact_edge(edge: dict[str, Any]) -> dict[str, Any]:
    keys = ("source", "target", "relation", "path", "line", "weight")
    return {key: edge[key] for key in keys if key in edge and edge[key] not in (None, "")}


def _artifact_dir_name() -> str:
    return os.getenv(ARTIFACT_DIR_ENV, DEFAULT_ARTIFACT_DIR)


def _workspace_roots() -> list[Path]:
    roots: list[Path] = []

    registry = os.getenv("GRAPHKV_WORKSPACE_REGISTRY")
    if registry:
        registry_path = Path(registry).expanduser()
        if registry_path.is_file():
            try:
                data = json.loads(registry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            records = data.get("workspaces", data if isinstance(data, list) else [])
            for record in records:
                root = record.get("root") if isinstance(record, dict) else record
                if isinstance(root, str):
                    roots.append(Path(root).expanduser())

    for env_name in ("GRAPHKV_WORKSPACE_ROOTS", "GRAPHKV_WORKSPACE_ROOT"):
        value = os.getenv(env_name)
        if not value:
            continue
        for raw_root in value.split(os.pathsep):
            if raw_root.strip():
                roots.append(Path(raw_root).expanduser())

    roots.append(Path.cwd())

    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            candidate = root.resolve()
        except OSError:
            continue
        key = candidate.as_posix()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(candidate)
    return resolved


@dataclass
class WorkspaceGraph:
    root: Path
    artifact_path: Path
    data: dict[str, Any]

    def __post_init__(self) -> None:
        self._nodes = list(self.data.get("nodes") or [])
        self._edges = list(self.data.get("edges") or [])
        self._communities = list(self.data.get("communities") or [])
        self._node_by_id = {str(node.get("id")): node for node in self._nodes}
        adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for edge in self._edges:
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if not source or not target:
                continue
            adjacency.setdefault(source, []).append((target, edge))
            adjacency.setdefault(target, []).append((source, edge))
        self._adjacency = adjacency
        self._search_rows = [
            (
                " ".join(str(node.get(key) or "") for key in ("id", "label", "kind", "path", "symbol", "language")),
                node,
            )
            for node in self._nodes
        ]

    @property
    def name(self) -> str:
        return str(self.data.get("workspace", {}).get("name") or self.root.name)

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self._nodes

    @property
    def edges(self) -> list[dict[str, Any]]:
        return self._edges

    @property
    def communities(self) -> list[dict[str, Any]]:
        return self._communities

    @property
    def node_by_id(self) -> dict[str, dict[str, Any]]:
        return self._node_by_id

    @property
    def adjacency(self) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        return self._adjacency

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root.as_posix(),
            "artifact_path": self.artifact_path.as_posix(),
            "built_at": self.data.get("workspace", {}).get("built_at"),
        }

    def match_nodes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = _tokenize(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for blob, node in self._search_rows:
            score = _score_blob(blob, terms)
            if query and str(node.get("id", "")).lower() == query.lower():
                score += 100.0
            if query and str(node.get("label", "")).lower() == query.lower():
                score += 80.0
            if query and str(node.get("path", "")).lower() == query.lower():
                score += 80.0
            if score > 0:
                scored.append((score, node))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("label") or item[1].get("id"))))
        return [node for _score, node in scored[:limit]]


_WORKSPACE_CACHE: dict[str, tuple[float, WorkspaceGraph]] = {}


def _load_workspace(root: Path) -> WorkspaceGraph | None:
    artifact_path = root / _artifact_dir_name() / "graph.json"
    if not artifact_path.is_file():
        return None
    try:
        mtime = artifact_path.stat().st_mtime
    except OSError:
        return None
    cache_key = artifact_path.as_posix()
    cached = _WORKSPACE_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    graph = WorkspaceGraph(root=root, artifact_path=artifact_path, data=data)
    _WORKSPACE_CACHE[cache_key] = (mtime, graph)
    return graph


def _all_workspaces() -> list[WorkspaceGraph]:
    workspaces: list[WorkspaceGraph] = []
    for root in _workspace_roots():
        loaded = _load_workspace(root)
        if loaded is not None:
            workspaces.append(loaded)
    return workspaces


def _resolve_workspace(workspace: str | None) -> WorkspaceGraph:
    workspaces = _all_workspaces()
    if not workspaces:
        roots = [root.as_posix() for root in _workspace_roots()]
        raise FileNotFoundError(f"no GraphKV artifacts found; run build-graphkv first for one of: {roots}")
    if not workspace:
        return workspaces[0]

    requested = workspace.lower()
    for candidate in workspaces:
        if requested in {candidate.name.lower(), candidate.root.as_posix().lower()}:
            return candidate
        if requested in candidate.root.as_posix().lower():
            return candidate
    known = ", ".join(item.name for item in workspaces)
    raise FileNotFoundError(f"unknown GraphKV workspace {workspace!r}; known: {known}")


mcp = FastMCP("GraphKV Local Search")


@mcp.tool()
def list_workspaces() -> dict[str, Any]:
    """List GraphKV workspaces that have generated graphkv-out artifacts."""
    roots = _workspace_roots()
    indexed = {workspace.root.as_posix(): workspace for workspace in _all_workspaces()}
    return {
        "ok": True,
        "artifact_dir": _artifact_dir_name(),
        "workspaces": [
            {
                "root": root.as_posix(),
                "name": indexed[root.as_posix()].name if root.as_posix() in indexed else root.name,
                "indexed": root.as_posix() in indexed,
                "artifact_path": (root / _artifact_dir_name() / "graph.json").as_posix(),
            }
            for root in roots
        ],
    }


@mcp.tool()
def workspace_index_status(workspace: str | None = None) -> dict[str, Any]:
    """Return generated artifact status for a GraphKV workspace."""
    if workspace:
        graph = _resolve_workspace(workspace)
        roots = [graph.root]
    else:
        roots = _workspace_roots()
    return {
        "ok": True,
        "artifact_dir": _artifact_dir_name(),
        "workspaces": [
            {
                "root": root.as_posix(),
                "graph_json": (root / _artifact_dir_name() / "graph.json").is_file(),
                "graph_report": (root / _artifact_dir_name() / "GRAPH_REPORT.md").is_file(),
                "graph_html": (root / _artifact_dir_name() / "graph.html").is_file(),
                "wiki_index": (root / _artifact_dir_name() / "wiki" / "index.md").is_file(),
            }
            for root in roots
        ],
    }


@mcp.tool()
def graph_stats(workspace: str | None = None) -> dict[str, Any]:
    """Return node, edge, community, and source-file counts for a GraphKV workspace."""
    graph = _resolve_workspace(workspace)
    stats = dict(graph.data.get("stats") or {})
    stats.setdefault("nodes", len(graph.nodes))
    stats.setdefault("edges", len(graph.edges))
    stats.setdefault("communities", len(graph.communities))
    return {"ok": True, "workspace": graph.summary(), "stats": stats}


@mcp.tool()
def god_nodes(limit: int = 10, workspace: str | None = None) -> dict[str, Any]:
    """Return the most connected GraphKV nodes for fast repo orientation."""
    graph = _resolve_workspace(workspace)
    limit = max(1, min(limit, 50))
    nodes = sorted(graph.nodes, key=lambda node: (-int(node.get("degree") or 0), str(node.get("label") or node.get("id"))))
    return {"ok": True, "workspace": graph.summary(), "nodes": [_compact_node(node) for node in nodes[:limit]]}


@mcp.tool()
def get_node(label: str, workspace: str | None = None) -> dict[str, Any]:
    """Find a GraphKV node by id, label, symbol, or path."""
    graph = _resolve_workspace(workspace)
    matches = graph.match_nodes(label, 12)
    return {"ok": bool(matches), "workspace": graph.summary(), "matches": [_compact_node(node) for node in matches]}


@mcp.tool()
def get_neighbors(label: str, relation_filter: str | None = None, workspace: str | None = None) -> dict[str, Any]:
    """Return direct neighbors and edge details for a GraphKV node."""
    graph = _resolve_workspace(workspace)
    matches = graph.match_nodes(label, 1)
    if not matches:
        return {"ok": False, "workspace": graph.summary(), "error": "node not found", "matches": []}
    node = matches[0]
    node_id = str(node.get("id"))
    node_by_id = graph.node_by_id
    neighbors: list[dict[str, Any]] = []
    for neighbor_id, edge in graph.adjacency.get(node_id, []):
        if relation_filter and edge.get("relation") != relation_filter:
            continue
        neighbor = node_by_id.get(neighbor_id)
        if neighbor is None:
            continue
        neighbors.append({"node": _compact_node(neighbor), "edge": _compact_edge(edge)})
    return {"ok": True, "workspace": graph.summary(), "node": _compact_node(node), "neighbors": neighbors}


@mcp.tool()
def get_community(community_id: int, workspace: str | None = None) -> dict[str, Any]:
    """Return all nodes in a generated GraphKV community."""
    graph = _resolve_workspace(workspace)
    node_by_id = graph.node_by_id
    for community in graph.communities:
        if int(community.get("id", -1)) != community_id:
            continue
        nodes = [node_by_id[node_id] for node_id in community.get("nodes", []) if node_id in node_by_id]
        return {
            "ok": True,
            "workspace": graph.summary(),
            "community": {key: community.get(key) for key in ("id", "label", "size")},
            "nodes": [_compact_node(node) for node in nodes],
        }
    return {"ok": False, "workspace": graph.summary(), "error": f"community {community_id} not found"}


@mcp.tool()
def query_graph(
    question: str,
    mode: str = "bfs",
    depth: int = 3,
    max_nodes: int = 30,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Search the GraphKV graph, then expand matching nodes by BFS or DFS."""
    graph = _resolve_workspace(workspace)
    mode = mode.lower().strip()
    if mode not in {"bfs", "dfs"}:
        raise ValueError("mode must be 'bfs' or 'dfs'")
    depth = max(0, min(depth, 8))
    max_nodes = max(1, min(max_nodes, 120))
    terms = _tokenize(question)
    node_by_id = graph.node_by_id
    adjacency = graph.adjacency
    seeds = graph.match_nodes(question, 8)
    if not seeds:
        seeds = sorted(graph.nodes, key=lambda node: -int(node.get("degree") or 0))[:4]

    included: set[str] = set()
    ordered: list[str] = []

    def include(node_id: str) -> bool:
        if node_id in included or node_id not in node_by_id or len(ordered) >= max_nodes:
            return False
        included.add(node_id)
        ordered.append(node_id)
        return True

    if mode == "bfs":
        queue: deque[tuple[str, int]] = deque()
        for seed in seeds:
            seed_id = str(seed.get("id"))
            if include(seed_id):
                queue.append((seed_id, 0))
        while queue and len(ordered) < max_nodes:
            node_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            neighbors = adjacency.get(node_id, [])
            neighbors = sorted(
                neighbors,
                key=lambda item: -_score_blob(" ".join(str(node_by_id.get(item[0], {}).get(key) or "") for key in ("label", "path", "symbol")), terms),
            )
            for neighbor_id, _edge in neighbors:
                if include(neighbor_id):
                    queue.append((neighbor_id, current_depth + 1))
                if len(ordered) >= max_nodes:
                    break
    else:
        stack: list[tuple[str, int]] = []
        for seed in reversed(seeds):
            seed_id = str(seed.get("id"))
            if include(seed_id):
                stack.append((seed_id, 0))
        while stack and len(ordered) < max_nodes:
            node_id, current_depth = stack.pop()
            if current_depth >= depth:
                continue
            neighbors = adjacency.get(node_id, [])
            ranked = sorted(
                neighbors,
                key=lambda item: _score_blob(" ".join(str(node_by_id.get(item[0], {}).get(key) or "") for key in ("label", "path", "symbol")), terms),
            )
            for neighbor_id, _edge in ranked:
                if include(neighbor_id):
                    stack.append((neighbor_id, current_depth + 1))
                if len(ordered) >= max_nodes:
                    break

    result_edges = [edge for edge in graph.edges if edge.get("source") in included and edge.get("target") in included]
    return {
        "ok": True,
        "workspace": graph.summary(),
        "question": question,
        "mode": mode,
        "depth": depth,
        "seeds": [_compact_node(node) for node in seeds],
        "nodes": [_compact_node(node_by_id[node_id]) for node_id in ordered],
        "edges": [_compact_edge(edge) for edge in result_edges[: max_nodes * 3]],
        "suggested_next_tools": ["get_node", "get_neighbors", "shortest_path", "get_community"],
    }


@mcp.tool()
def shortest_path(source: str, target: str, max_hops: int = 8, workspace: str | None = None) -> dict[str, Any]:
    """Find the shortest path between two GraphKV concepts."""
    graph = _resolve_workspace(workspace)
    max_hops = max(1, min(max_hops, 16))
    sources = graph.match_nodes(source, 5)
    targets = {str(node.get("id")) for node in graph.match_nodes(target, 8)}
    node_by_id = graph.node_by_id
    if not sources or not targets:
        return {"ok": False, "workspace": graph.summary(), "error": "source or target not found"}

    for source_node in sources:
        source_id = str(source_node.get("id"))
        queue: deque[tuple[str, list[str], list[dict[str, Any]]]] = deque([(source_id, [source_id], [])])
        seen = {source_id}
        while queue:
            node_id, path, path_edges = queue.popleft()
            if node_id in targets:
                return {
                    "ok": True,
                    "workspace": graph.summary(),
                    "nodes": [_compact_node(node_by_id[item]) for item in path],
                    "edges": [_compact_edge(edge) for edge in path_edges],
                    "hops": len(path) - 1,
                }
            if len(path) - 1 >= max_hops:
                continue
            for neighbor_id, edge in graph.adjacency.get(node_id, []):
                if neighbor_id in seen:
                    continue
                seen.add(neighbor_id)
                queue.append((neighbor_id, [*path, neighbor_id], [*path_edges, edge]))
    return {"ok": False, "workspace": graph.summary(), "error": "no path found within max_hops"}


@mcp.tool()
def search_symbols(query: str, max_results: int = 20, workspace: str | None = None) -> dict[str, Any]:
    """Search symbol and heading nodes without forcing source browsing or edits."""
    graph = _resolve_workspace(workspace)
    max_results = max(1, min(max_results, 80))
    matches = [node for node in graph.match_nodes(query, max_results * 2) if node.get("kind") in {"symbol", "heading", "command", "agent", "skill", "mcp"}]
    return {"ok": True, "workspace": graph.summary(), "matches": [_compact_node(node) for node in matches[:max_results]]}


if __name__ == "__main__":
    mcp.run(transport=os.getenv("GRAPHKV_MCP_TRANSPORT", "stdio"))