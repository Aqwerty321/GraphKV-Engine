#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "target",
    "bazel-bin",
    "bazel-out",
    "bazel-testlogs",
}

DOC_EXTENSIONS = {
    ".adoc",
    ".ipynb",
    ".md",
    ".mdx",
    ".rst",
    ".txt",
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
    ".m",
    ".mm",
    ".php",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}

LANGUAGE_BY_EXTENSION = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c-cpp-header",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript-react",
    ".kt": "kotlin",
    ".lua": "lua",
    ".m": "objective-c",
    ".md": "markdown",
    ".mdx": "mdx",
    ".mm": "objective-cpp",
    ".php": "php",
    ".proto": "protobuf",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".rst": "rst",
    ".scala": "scala",
    ".sh": "shell",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript-react",
    ".yaml": "yaml",
    ".yml": "yaml",
}

LINK_PATTERN = re.compile(r"(?P<path>[\w./-]+\.(?:c|cc|cpp|h|hpp|go|java|js|jsx|md|mdx|py|rs|ts|tsx|yaml|yml))|(?P<symbol>`?[A-Za-z_][A-Za-z0-9_:.#/-]{2,}`?)")
HEADING_PATTERN = re.compile(r"^(#{1,6}|={2,}|-{2,})\s*(?P<title>.+?)\s*$")


def guess_language(path: Path) -> str:
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), path.suffix.lower().lstrip("."))


def media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".mdx"}:
        return "text/markdown"
    if suffix == ".rst":
        return "text/x-rst"
    if suffix in SOURCE_EXTENSIONS:
        return "text/source"
    return "text/plain"


def iter_files(root: Path, max_files: int, max_bytes: int) -> Iterable[Path]:
    emitted = 0
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in DOC_EXTENSIONS and path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        yield path
        emitted += 1
        if emitted >= max_files:
            return


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def title_for(path: Path, text: str) -> str:
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.name
    return path.name


def event(**values: object) -> str:
    return json.dumps({key: value for key, value in values.items() if value not in (None, "")}, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def chunk_markdown(text: str, target_chars: int) -> Iterable[tuple[str, int, int, str]]:
    heading = ""
    current: list[str] = []
    start_line = 1
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = HEADING_PATTERN.match(line.strip())
        if match and current:
            yield heading, start_line, line_number - 1, "\n".join(current).strip()
            current = []
            start_line = line_number
        if match:
            heading = match.group("title")
        if not current:
            start_line = line_number
        current.append(line)
        if sum(len(item) + 1 for item in current) >= target_chars:
            yield heading, start_line, line_number, "\n".join(current).strip()
            current = []
    if current:
        yield heading, start_line, start_line + len(current) - 1, "\n".join(current).strip()


def chunk_source_comments(text: str, target_chars: int) -> Iterable[tuple[str, int, int, str]]:
    block: list[str] = []
    start_line = 1
    in_block = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        is_comment = stripped.startswith(("#", "//", "*", "/*", "<!--")) or stripped.startswith(('"""', "'''")) or in_block
        if stripped.startswith(("/*", '"""', "'''", "<!--")):
            in_block = True
        if is_comment:
            if not block:
                start_line = line_number
            block.append(line)
        elif block:
            text_block = "\n".join(block).strip()
            if len(text_block) >= 24:
                yield "source comments", start_line, line_number - 1, text_block
            block = []
        if in_block and ("*/" in stripped or stripped.endswith(('"""', "'''", "-->"))):
            in_block = False
        if sum(len(item) + 1 for item in block) >= target_chars:
            yield "source comments", start_line, line_number, "\n".join(block).strip()
            block = []
            in_block = False
    if block:
        text_block = "\n".join(block).strip()
        if len(text_block) >= 24:
            yield "source comments", start_line, start_line + len(block) - 1, text_block


def links_for(path: str, text: str, line_start: int, limit: int) -> Iterable[tuple[str, str, int]]:
    seen: set[str] = set()
    for match in LINK_PATTERN.finditer(text):
        target = (match.group("path") or match.group("symbol") or "").strip("`")
        if not target or target in seen or target == path:
            continue
        seen.add(target)
        link_type = "path_mention" if "." in target or "/" in target else "symbol_mention"
        yield target, link_type, line_start
        if len(seen) >= limit:
            return


def emit_file_events(root: Path, path: Path, target_chars: int, link_limit: int) -> Iterable[str]:
    text = read_text(path)
    if text is None or not text.strip():
        return
    relative = f"{root.name}/{path.relative_to(root).as_posix()}"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    yield event(
        type="doc_start",
        path=relative,
        media_type=media_type(path),
        language=guess_language(path),
        parser="graphkv-build-doc-bundle-v1",
        title=title_for(path, text),
        content_hash=digest,
    )
    suffix = path.suffix.lower()
    chunks = chunk_markdown(text, target_chars) if suffix in DOC_EXTENSIONS else chunk_source_comments(text, target_chars)
    emitted_chunks = 0
    for heading, line_start, line_end, chunk_text in chunks:
        if not chunk_text:
            continue
        chunk_type = "markdown" if suffix in DOC_EXTENSIONS else "source_comment"
        yield event(
            type="chunk",
            path=relative,
            heading=heading,
            chunk_type=chunk_type,
            text=chunk_text,
            line_start=line_start,
            line_end=line_end,
            token_estimate=estimate_tokens(chunk_text),
        )
        for target, link_type, link_line in links_for(relative, chunk_text, line_start, link_limit):
            yield event(type="link", path=relative, target=target, link_type=link_type, line_start=link_line, confidence=700)
        emitted_chunks += 1
    if emitted_chunks == 0 and suffix in DOC_EXTENSIONS:
        preview = text.strip()[:target_chars]
        yield event(
            type="chunk",
            path=relative,
            heading=title_for(path, text),
            chunk_type="text",
            text=preview,
            line_start=1,
            line_end=min(len(text.splitlines()), 80),
            token_estimate=estimate_tokens(preview),
        )
    yield event(type="doc_end", path=relative)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a normalized GraphKV document NDJSON bundle from mixed-language repos.")
    parser.add_argument("repos", nargs="+", type=Path, help="repository roots to scan")
    parser.add_argument("--output", type=Path, required=True, help="NDJSON bundle output path")
    parser.add_argument("--max-files", type=int, default=400)
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024)
    parser.add_argument("--target-chars", type=int, default=2600)
    parser.add_argument("--link-limit", type=int, default=12)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    event_count = 0
    source_count = 0
    with args.output.open("w", encoding="utf-8") as output:
        for repo in args.repos:
            root = repo.resolve()
            if not root.is_dir():
                raise RuntimeError(f"repo root does not exist: {root}")
            for path in iter_files(root, args.max_files, args.max_file_bytes):
                before = event_count
                for line in emit_file_events(root, path, args.target_chars, args.link_limit):
                    output.write(line)
                    output.write("\n")
                    event_count += 1
                if event_count != before:
                    source_count += 1
    print(json.dumps({"output": str(args.output), "sources": source_count, "events": event_count}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())