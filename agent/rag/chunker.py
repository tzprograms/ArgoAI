# Structure-aware document chunking for markdown and Go source files.

import hashlib
import os
import re
import logging

logger = logging.getLogger(__name__)

_GO_DECL_RE = re.compile(r"^(?:func\s|type\s|var\s|const\s)", re.MULTILINE)


def chunk_document(content: str, source: str, max_chunk_size: int = 1500) -> list[dict]:
    # Splitting .md file in chunks preserving structure
    sections = _split_by_headings(content)
    chunks = []

    for sec in sections:
        body = sec["body"].strip()
        if not body:
            continue

        text = f"{sec['heading']}\n{body}" if sec["heading"] else body

        if len(text) <= max_chunk_size:
            chunks.append({
                "id": _chunk_id(source, sec["heading"], 0),
                "content": text,
                "source": source,
                "title": sec["heading"],
            })
        else:
            parts = _split_large(body, max_chunk_size)
            for i, part in enumerate(parts):
                full = f"{sec['heading']}\n{part}" if sec["heading"] else part
                chunks.append({
                    "id": _chunk_id(source, sec["heading"], i),
                    "content": full,
                    "source": source,
                    "title": sec["heading"],
                })

    return chunks


def chunk_go_file(content: str, source: str, max_chunk_size: int = 1500) -> list[dict]:
    """Split a Go source file by top-level declarations (func, type, var, const)."""
    lines = content.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if _GO_DECL_RE.match(line):
            body_start = i
            break
    else:
        body_start = len(lines)

    body = "\n".join(lines[body_start:])
    positions = [m.start() for m in _GO_DECL_RE.finditer(body)]
    if not positions:
        text = content.strip()
        if not text:
            return []
        return [{"id": _chunk_id(source, "file", 0), "content": text[:max_chunk_size], "source": source, "title": source}]

    chunks = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(body)
        block = body[pos:end].strip()
        if not block:
            continue

        first_line = block.split("\n", 1)[0]
        title = f"{source}: {first_line[:80]}"

        if len(block) <= max_chunk_size:
            chunks.append({"id": _chunk_id(source, first_line, 0), "content": block, "source": source, "title": title})
        else:
            parts = _split_large(block, max_chunk_size)
            for j, part in enumerate(parts):
                chunks.append({"id": _chunk_id(source, first_line, j), "content": part, "source": source, "title": title})

    return chunks


_ALLOWED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".go"}


def chunk_directory(directory: str, max_chunk_size: int = 1500) -> list[dict]:
    """Walk a directory and chunk all supported files."""
    all_chunks = []
    for root, _, files in os.walk(directory):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _ALLOWED_EXTENSIONS:
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                rel_path = os.path.relpath(path, directory)
                if ext == ".go":
                    chunks = chunk_go_file(content, rel_path, max_chunk_size)
                else:
                    chunks = chunk_document(content, rel_path, max_chunk_size)
                all_chunks.extend(chunks)
                logger.info(f"Chunked {rel_path}: {len(chunks)} chunks")
            except Exception as e:
                logger.warning(f"Failed to read {path}: {e}")
    return all_chunks


def _split_by_headings(content: str) -> list[dict]:
    lines = content.split("\n")
    sections = []
    current = {"heading": "", "body": ""}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if current["heading"] or current["body"]:
                sections.append(current)
            current = {"heading": stripped, "body": ""}
        else:
            current["body"] += line + "\n"

    if current["heading"] or current["body"]:
        sections.append(current)
    return sections


def _split_large(body: str, max_size: int) -> list[str]:
    paragraphs = body.split("\n\n")
    parts = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 2 > max_size:
            parts.append(current)
            current = ""
        if current:
            current += "\n\n"
        current += para

    if current:
        parts.append(current)
    return parts


def _chunk_id(source: str, heading: str, idx: int) -> str:
    h = hashlib.sha256(f"{source}::{heading}::{idx}".encode()).hexdigest()[:24]
    return h
