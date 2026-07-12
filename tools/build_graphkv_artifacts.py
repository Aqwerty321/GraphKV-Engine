#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fnmatch import fnmatchcase
import html
import json
import os
import re
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SKIP_DIRS = {
    ".cache",
    ".git",
    ".graphkv",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".turbo",
    ".venv",
    "_deps",
    "__pycache__",
    "build",
    "coverage",
    "data",
    "dist",
    "field-trials",
    "graphify-out",
    "graphkv-out",
    "htmlcov",
    "local_repos",
    "node_modules",
    "out",
    "results",
    "target",
    "tmp",
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
MAX_FILE_BYTES = 512 * 1024
SYMBOL_RE = re.compile(
    r"^\s*(?:(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var|interface|type)\s+|"
    r"(?:pub\s+)?(?:async\s+)?fn\s+|(?:pub\s+)?(?:struct|enum|trait|impl)\s+)([A-Za-z_][A-Za-z0-9_]*)"
)
CPP_FUNCTION_RE = re.compile(
    r"^\s*(?:(?:inline|static|constexpr|consteval|virtual|explicit)\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_:<>~*&]*\s+)+([A-Za-z_~][A-Za-z0-9_:~]*)\s*\([^;]*\)"
    r"(?:\s*(?:const|noexcept|override|final))*\s*(?:\{|$)"
)
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
LOCAL_IMPORT_RE = re.compile(r"(?:from\s+['\"](?P<from>\.{1,2}/[^'\"]+)['\"]|import\s+.*?from\s+['\"](?P<import>\.{1,2}/[^'\"]+)['\"]|require\(['\"](?P<require>\.{1,2}/[^'\"]+)['\"]\))")
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import|import\s+([A-Za-z_][A-Za-z0-9_.]*))")
INCLUDE_RE = re.compile(r"^\s*#\s*include\s+\"([^\"]+)\"")
MCP_NAME_RE = re.compile(r"\b([a-z][a-z0-9-]*-local)\b")
SLASH_COMMAND_RE = re.compile(r"(?<!\w)/(?:[a-z][a-z0-9-]*)")


@dataclass
class IgnoreStats:
    ignored_dirs: int = 0
    ignored_files: int = 0


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def language_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".md": "markdown",
        ".mdx": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".sh": "shell",
    }.get(suffix, suffix.removeprefix(".") or "text")


def safe_node_id(prefix: str, value: str) -> str:
    return f"{prefix}:{value.replace(os.sep, '/')}"


def normalize_ignore_pattern(pattern: str) -> str:
    normalized = pattern.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def read_ignore_patterns(root: Path, ignore_file: Path | None, extra_patterns: list[str]) -> list[str]:
    patterns: list[str] = []
    source = ignore_file if ignore_file is not None else root / ".graphkvignore"
    if source.exists():
        for raw_line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    patterns.extend(pattern for pattern in extra_patterns if pattern.strip())
    return patterns


def ignore_pattern_matches(relative_path: str, pattern: str, is_dir: bool) -> bool:
    raw_pattern = normalize_ignore_pattern(pattern)
    if not raw_pattern:
        return False

    directory_only = raw_pattern.endswith("/")
    pattern_body = raw_pattern.rstrip("/")
    if directory_only and not is_dir:
        return False

    if "/" not in pattern_body:
        parts = relative_path.split("/")
        return any(fnmatchcase(part, pattern_body) for part in parts)

    if fnmatchcase(relative_path, pattern_body) or fnmatchcase(relative_path, f"{pattern_body}/**"):
        return True
    return relative_path == pattern_body or relative_path.startswith(f"{pattern_body}/")


def ignored_by_patterns(relative_path: str, is_dir: bool, patterns: list[str]) -> bool:
    ignored = False
    for pattern in patterns:
        normalized = pattern.strip()
        if not normalized or normalized.startswith("#"):
            continue
        negated = normalized.startswith("!")
        candidate = normalized[1:] if negated else normalized
        if ignore_pattern_matches(relative_path, candidate, is_dir):
            ignored = not negated
    return ignored


def iter_source_files(root: Path, max_files: int, ignore_patterns: list[str]) -> tuple[list[Path], bool, IgnoreStats]:
    files: list[Path] = []
    truncated = False
    stats = IgnoreStats()
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        current_relative = current_path.relative_to(root).as_posix() if current_path != root else ""
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            relative_dir = f"{current_relative}/{name}" if current_relative else name
            if name in SKIP_DIRS or name.startswith(".tox") or name.startswith("build-"):
                stats.ignored_dirs += 1
                continue
            if ignored_by_patterns(relative_dir, True, ignore_patterns):
                stats.ignored_dirs += 1
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            path = Path(current) / filename
            relative_file = path.relative_to(root).as_posix()
            if ignored_by_patterns(relative_file, False, ignore_patterns):
                stats.ignored_files += 1
                continue
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
            if len(files) >= max_files:
                return files, True, stats
    return files, truncated, stats


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in data[:4096]:
        return ""
    return data.decode("utf-8", errors="replace")


def community_label_for(relative_path: str, language: str) -> str:
    parts = Path(relative_path).parts
    if parts and parts[0] == ".opencode" and len(parts) > 1:
        return f"opencode/{parts[1]}"
    if parts and parts[0] in {"docs", ".github", "mcp-server", "tools", "src", "include", "tests", "benchmarks"}:
        return parts[0]
    if len(parts) > 1:
        return parts[0]
    return language or "root"


class GraphBuilder:
    def __init__(self, root: Path, max_files: int, ignore_patterns: list[str]) -> None:
        self.root = root
        self.max_files = max_files
        self.ignore_patterns = ignore_patterns
        self.ignore_stats = IgnoreStats()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.symbol_index: dict[str, list[str]] = defaultdict(list)
        self.path_index: dict[str, str] = {}
        self.community_ids: dict[str, int] = {}
        self.scan_truncated = False

    def add_node(self, node_id: str, **data: Any) -> None:
        existing = self.nodes.get(node_id, {})
        existing.update({"id": node_id, **data})
        self.nodes[node_id] = existing

    def add_edge(self, source: str, target: str, relation: str, **data: Any) -> None:
        if source == target:
            return
        key = (source, target, relation)
        if key in self.edges:
            return
        self.edges[key] = {"source": source, "target": target, "relation": relation, **data}

    def community_id(self, label: str) -> int:
        if label not in self.community_ids:
            self.community_ids[label] = len(self.community_ids)
        return self.community_ids[label]

    def build(self) -> dict[str, Any]:
        files, self.scan_truncated, self.ignore_stats = iter_source_files(self.root, self.max_files, self.ignore_patterns)
        file_texts: dict[str, str] = {}

        for path in files:
            relative = path.relative_to(self.root).as_posix()
            text = read_text(path)
            file_texts[relative] = text
            language = language_for(path)
            community_label = community_label_for(relative, language)
            community = self.community_id(community_label)
            file_id = safe_node_id("file", relative)
            self.path_index[relative] = file_id
            self.add_node(
                file_id,
                label=relative,
                kind="file",
                path=relative,
                language=language,
                community=community,
                bytes=len(text.encode("utf-8", errors="replace")),
            )
            self.extract_symbols(path, relative, text, language, community, file_id)
            self.extract_named_entities(relative, text, community, file_id)

        for relative, text in file_texts.items():
            self.extract_import_edges(relative, text)
        self.extract_symbol_reference_edges(file_texts)
        self.attach_degrees()
        return self.to_json()

    def extract_symbols(self, path: Path, relative: str, text: str, language: str, community: int, file_id: str) -> None:
        for line_number, line in enumerate(text.splitlines(), start=1):
            symbol: str | None = None
            kind = "symbol"
            if language == "markdown":
                match = MARKDOWN_HEADING_RE.match(line)
                if match:
                    symbol = match.group(2).strip().strip("#")[:120]
                    kind = "heading"
            else:
                match = SYMBOL_RE.match(line) or CPP_FUNCTION_RE.match(line)
                if match:
                    symbol = match.group(1).split("::")[-1]
            if not symbol:
                continue
            symbol_id = safe_node_id(kind, f"{relative}:{symbol}:{line_number}")
            self.add_node(
                symbol_id,
                label=f"{symbol}()" if kind == "symbol" else symbol,
                kind=kind,
                path=relative,
                line=line_number,
                symbol=symbol,
                language=language,
                community=community,
            )
            self.symbol_index[symbol.lower()].append(symbol_id)
            self.add_edge(file_id, symbol_id, "defines", path=relative, line=line_number, weight=2.0)

        if ".opencode/commands/" in relative and relative.endswith(".md"):
            command = f"/{Path(relative).stem}"
            command_id = safe_node_id("command", command)
            self.add_node(command_id, label=f"Command: {command}", kind="command", path=relative, community=community)
            self.add_edge(file_id, command_id, "defines_command", path=relative, weight=2.0)
        if ".opencode/agents/" in relative and relative.endswith(".md"):
            agent = Path(relative).stem
            agent_id = safe_node_id("agent", agent)
            self.add_node(agent_id, label=f"Agent: {agent}", kind="agent", path=relative, community=community)
            self.add_edge(file_id, agent_id, "defines_agent", path=relative, weight=2.0)
        if "/SKILL.md" in relative or relative.endswith("SKILL.md"):
            skill = Path(relative).parent.name if Path(relative).name == "SKILL.md" else Path(relative).stem
            skill_id = safe_node_id("skill", skill)
            self.add_node(skill_id, label=f"Skill: {skill}", kind="skill", path=relative, community=community)
            self.add_edge(file_id, skill_id, "defines_skill", path=relative, weight=2.0)

    def extract_named_entities(self, relative: str, text: str, community: int, file_id: str) -> None:
        for name in sorted(set(MCP_NAME_RE.findall(text))):
            node_id = safe_node_id("mcp", name)
            self.add_node(node_id, label=f"MCP: {name}", kind="mcp", path=relative, community=community)
            self.add_edge(file_id, node_id, "mentions_mcp", path=relative, weight=1.2)
        for command in sorted(set(SLASH_COMMAND_RE.findall(text))):
            if len(command) > 60:
                continue
            node_id = safe_node_id("command", command)
            self.add_node(node_id, label=f"Command: {command}", kind="command", path=relative, community=community)
            self.add_edge(file_id, node_id, "mentions_command", path=relative, weight=1.0)

    def resolve_relative_import(self, source_relative: str, raw_target: str) -> str | None:
        source_dir = (self.root / source_relative).parent
        base = (source_dir / raw_target).resolve()
        candidates = [base]
        for suffix in SOURCE_EXTENSIONS:
            candidates.append(base.with_suffix(suffix))
        candidates.extend(base / f"index{suffix}" for suffix in (".ts", ".tsx", ".js", ".jsx"))
        for candidate in candidates:
            try:
                relative = candidate.relative_to(self.root).as_posix()
            except ValueError:
                continue
            if relative in self.path_index:
                return relative
        return None

    def extract_import_edges(self, relative: str, text: str) -> None:
        source_id = self.path_index.get(relative)
        if not source_id:
            return
        for line_number, line in enumerate(text.splitlines(), start=1):
            local_match = LOCAL_IMPORT_RE.search(line)
            if local_match:
                raw = next(value for value in local_match.groupdict().values() if value)
                target_relative = self.resolve_relative_import(relative, raw)
                if target_relative and target_relative in self.path_index:
                    self.add_edge(source_id, self.path_index[target_relative], "imports", path=relative, line=line_number, weight=1.6)
            py_match = PY_IMPORT_RE.match(line)
            if py_match:
                module = (py_match.group(1) or py_match.group(2) or "").split(".")[0]
                for candidate in (f"{module}.py", f"{module}/__init__.py"):
                    if candidate in self.path_index:
                        self.add_edge(source_id, self.path_index[candidate], "imports", path=relative, line=line_number, weight=1.6)
            include_match = INCLUDE_RE.match(line)
            if include_match:
                target_relative = self.resolve_relative_import(relative, include_match.group(1))
                if target_relative and target_relative in self.path_index:
                    self.add_edge(source_id, self.path_index[target_relative], "includes", path=relative, line=line_number, weight=1.6)

    def extract_symbol_reference_edges(self, file_texts: dict[str, str]) -> None:
        indexed_symbols = {name: ids for name, ids in self.symbol_index.items() if len(name) >= 3}
        for relative, text in file_texts.items():
            source_id = self.path_index.get(relative)
            if not source_id:
                continue
            lower = text.lower()
            emitted = 0
            for name, symbol_ids in indexed_symbols.items():
                if emitted >= 64:
                    break
                if name not in lower:
                    continue
                for symbol_id in symbol_ids[:3]:
                    target = self.nodes.get(symbol_id, {})
                    if target.get("path") == relative:
                        continue
                    self.add_edge(source_id, symbol_id, "references", path=relative, weight=0.6)
                    emitted += 1
                    if emitted >= 64:
                        break

    def attach_degrees(self) -> None:
        degree: Counter[str] = Counter()
        for edge in self.edges.values():
            degree[str(edge["source"])] += 1
            degree[str(edge["target"])] += 1
        for node_id, count in degree.items():
            if node_id in self.nodes:
                self.nodes[node_id]["degree"] = count
        for node in self.nodes.values():
            node.setdefault("degree", 0)

    def to_json(self) -> dict[str, Any]:
        community_nodes: dict[int, list[str]] = defaultdict(list)
        for node in self.nodes.values():
            community_nodes[int(node.get("community") or 0)].append(str(node["id"]))
        labels_by_id = {value: key for key, value in self.community_ids.items()}
        communities = [
            {"id": community_id, "label": labels_by_id.get(community_id, f"community-{community_id}"), "size": len(nodes), "nodes": sorted(nodes)}
            for community_id, nodes in sorted(community_nodes.items(), key=lambda item: (-len(item[1]), item[0]))
        ]
        kind_counts = Counter(str(node.get("kind") or "unknown") for node in self.nodes.values())
        language_counts = Counter(str(node.get("language") or "unknown") for node in self.nodes.values() if node.get("kind") == "file")
        return {
            "schema": "graphkv-artifacts-v1",
            "workspace": {"root": self.root.as_posix(), "name": self.root.name, "built_at": now_iso()},
            "stats": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "communities": len(communities),
                "files": kind_counts.get("file", 0),
                "symbols": kind_counts.get("symbol", 0),
                "headings": kind_counts.get("heading", 0),
                "scan_truncated": self.scan_truncated,
                "ignored_dirs": self.ignore_stats.ignored_dirs,
                "ignored_files": self.ignore_stats.ignored_files,
                "ignore_patterns": self.ignore_patterns,
                "kind_counts": dict(sorted(kind_counts.items())),
                "language_counts": dict(sorted(language_counts.items())),
            },
            "nodes": sorted(self.nodes.values(), key=lambda node: str(node.get("id"))),
            "edges": sorted(self.edges.values(), key=lambda edge: (str(edge.get("source")), str(edge.get("relation")), str(edge.get("target")))),
            "communities": communities,
        }


def compact_node(node: dict[str, Any]) -> str:
    path = node.get("path")
    line = node.get("line")
    suffix = f" ({path}:{line})" if path and line else f" ({path})" if path else ""
    return f"`{node.get('label') or node.get('id')}` - {node.get('kind')} degree {node.get('degree', 0)}{suffix}"


def write_report(graph: dict[str, Any], output_dir: Path) -> None:
    stats = graph["stats"]
    nodes = sorted(graph["nodes"], key=lambda node: (-int(node.get("degree") or 0), str(node.get("label") or node.get("id"))))
    lines = [
        f"# GraphKV Report - {graph['workspace']['root']} ({graph['workspace']['built_at']})",
        "",
        "## Summary",
        f"- {stats['nodes']} nodes · {stats['edges']} edges · {stats['communities']} communities detected",
        f"- Files: {stats['files']} · symbols: {stats['symbols']} · headings: {stats['headings']}",
        f"- Scan truncated: {stats['scan_truncated']}",
        f"- Ignored by defaults or `.graphkvignore`: {stats.get('ignored_dirs', 0)} dirs · {stats.get('ignored_files', 0)} files",
        "",
        "## God Nodes",
    ]
    for index, node in enumerate(nodes[:15], start=1):
        lines.append(f"{index}. {compact_node(node)}")
    lines.extend(["", "## Communities"])
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    for community in graph["communities"][:20]:
        lines.append("")
        lines.append(f"### Community {community['id']} - {community['label']}")
        picks = [node_by_id[node_id] for node_id in community["nodes"] if node_id in node_by_id]
        picks = sorted(picks, key=lambda node: (-int(node.get("degree") or 0), str(node.get("label") or node.get("id"))))[:12]
        lines.append("Nodes: " + ", ".join(f"`{node.get('label') or node.get('id')}`" for node in picks))
    lines.extend(
        [
            "",
            "## Suggested Questions",
            "- What are the most connected files or symbols in this workspace?",
            "- Which neighbors explain the role of a named command, MCP, file, or symbol?",
            "- What shortest path connects two implementation concepts?",
        ]
    )
    (output_dir / "GRAPH_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_wiki(graph: dict[str, Any], output_dir: Path) -> None:
    wiki_dir = output_dir / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    index_lines = [f"# GraphKV Wiki - {graph['workspace']['name']}", "", "## Communities"]
    for community in graph["communities"]:
        filename = f"community-{community['id']}.md"
        index_lines.append(f"- [Community {community['id']} - {community['label']}]({filename}) ({community['size']} nodes)")
        page_lines = [f"# Community {community['id']} - {community['label']}", ""]
        for node_id in community["nodes"]:
            node = node_by_id.get(node_id)
            if not node:
                continue
            page_lines.append(f"- {compact_node(node)}")
        (wiki_dir / filename).write_text("\n".join(page_lines) + "\n", encoding="utf-8")
    (wiki_dir / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")


def write_html(graph: dict[str, Any], output_dir: Path) -> None:
    top_nodes = sorted(graph["nodes"], key=lambda node: -int(node.get("degree") or 0))[:160]
    top_ids = {node["id"] for node in top_nodes}
    edges = [edge for edge in graph["edges"] if edge["source"] in top_ids and edge["target"] in top_ids][:420]
    payload = json.dumps({"nodes": top_nodes, "edges": edges}, separators=(",", ":"))
    document = f"""<!doctype html>
<html lang=\"en\">
<meta charset=\"utf-8\">
<title>GraphKV Graph - {html.escape(graph['workspace']['name'])}</title>
<style>
body {{ margin: 0; font: 14px system-ui, sans-serif; background: #101216; color: #e8edf2; }}
header {{ padding: 16px 20px; border-bottom: 1px solid #2a3038; }}
#graph {{ width: 100vw; height: calc(100vh - 70px); display: block; }}
.hint {{ color: #9aa6b2; }}
</style>
<header><strong>GraphKV Graph</strong> <span class=\"hint\">{html.escape(graph['workspace']['root'])}</span></header>
<canvas id=\"graph\"></canvas>
<script>
const graph = {payload};
const canvas = document.getElementById('graph');
const ctx = canvas.getContext('2d');
function resize() {{ canvas.width = innerWidth * devicePixelRatio; canvas.height = (innerHeight - 70) * devicePixelRatio; draw(); }}
function draw() {{
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, radius = Math.min(w, h) * 0.38;
  const positions = new Map();
  graph.nodes.forEach((node, index) => {{
    const angle = (Math.PI * 2 * index) / Math.max(1, graph.nodes.length);
    const degree = Math.max(1, node.degree || 1);
    const r = radius * (0.45 + 0.5 / Math.sqrt(degree));
    positions.set(node.id, [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r]);
  }});
  ctx.lineWidth = devicePixelRatio;
  ctx.strokeStyle = 'rgba(128, 160, 190, 0.22)';
  graph.edges.forEach(edge => {{
    const a = positions.get(edge.source), b = positions.get(edge.target);
    if (!a || !b) return;
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
  }});
  graph.nodes.forEach(node => {{
    const p = positions.get(node.id); if (!p) return;
    const degree = Math.max(1, node.degree || 1);
    const size = Math.min(14, 4 + Math.sqrt(degree) * 2) * devicePixelRatio;
    ctx.fillStyle = node.kind === 'file' ? '#6ec6ff' : node.kind === 'mcp' ? '#ffd166' : '#8bd17c';
    ctx.beginPath(); ctx.arc(p[0], p[1], size, 0, Math.PI * 2); ctx.fill();
  }});
  ctx.fillStyle = '#dce6ef'; ctx.font = `${{12 * devicePixelRatio}}px system-ui`;
  graph.nodes.slice(0, 30).forEach(node => {{ const p = positions.get(node.id); if (p) ctx.fillText(node.label || node.id, p[0] + 8, p[1] - 8); }});
}}
addEventListener('resize', resize); resize();
</script>
</html>
"""
    (output_dir / "graph.html").write_text(document, encoding="utf-8")


def write_artifacts(graph: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "graph.json").write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    write_report(graph, output_dir)
    write_wiki(graph, output_dir)
    write_html(graph, output_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build GraphKV search artifacts for one workspace")
    parser.add_argument("workspace", nargs="?", default=".", help="Workspace root to index")
    parser.add_argument("--output-dir", default=None, help="Artifact directory; defaults to <workspace>/graphkv-out")
    parser.add_argument("--max-files", type=int, default=2500, help="Maximum source files to scan")
    parser.add_argument("--ignore-file", default=None, help="Ignore file to read; defaults to <workspace>/.graphkvignore")
    parser.add_argument("--ignore", action="append", default=[], help="Additional repo-relative ignore pattern; may be repeated")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.workspace).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"workspace not found: {root}")
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else root / "graphkv-out"
    ignore_file = Path(args.ignore_file).expanduser().resolve() if args.ignore_file else None
    ignore_patterns = read_ignore_patterns(root, ignore_file, list(args.ignore or []))
    graph = GraphBuilder(root, max(1, args.max_files), ignore_patterns).build()
    write_artifacts(graph, output_dir)
    print(f"Built GraphKV artifacts in {output_dir}")
    print(f"Nodes: {graph['stats']['nodes']}  Edges: {graph['stats']['edges']}  Communities: {graph['stats']['communities']}")
    print(f"Ignored: {graph['stats']['ignored_dirs']} dirs  {graph['stats']['ignored_files']} files  Patterns: {len(ignore_patterns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())