"""Splits Markdown documents into chunks for retrieval.

Sections follow the document's own headings, so each chunk keeps the heading
it belongs to (used for citations). Sections longer than the chunk size are
split on paragraph boundaries, with some overlap so a fact cut in two still
shows up whole in one of the pieces. No external dependencies, so it's unit
tested without the embedding model or Qdrant.
"""
import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    source: str  # file path relative to the corpus root
    heading: str  # nearest Markdown heading, or the file name
    text: str


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """(heading, body) pairs in document order. `#` inside code fences
    (shell comments) isn't a heading; empty sections are dropped."""
    sections: list[tuple[str, str]] = []
    heading, lines, in_code = "", [], False

    def flush() -> None:
        # The heading line alone isn't content.
        body = lines[1:] if heading else lines
        if "\n".join(body).strip():
            sections.append((heading, "\n".join(lines).strip()))

    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        match = None if in_code else HEADING.match(line)
        if match:
            flush()
            heading, lines = match.group(2), [line]
        else:
            lines.append(line)
    flush()
    return sections


def window(text: str, size: int, overlap: int) -> list[str]:
    """Pieces of about `size` characters, cut between paragraphs when
    possible. Each piece after the first starts with the last `overlap`
    characters of the previous one, so it can run up to size + overlap."""
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            pieces.append(current)
        # A single paragraph longer than `size` is cut at fixed offsets.
        while len(para) > size:
            pieces.append(para[:size])
            para = para[size - overlap:]
        tail = pieces[-1][-overlap:] if pieces and overlap else ""
        current = f"{tail}\n\n{para}" if tail else para
    if current:
        pieces.append(current)
    return pieces


def chunk_document(source: str, markdown: str, size: int = 1000, overlap: int = 150) -> list[Chunk]:
    return [
        Chunk(source=source, heading=heading or source, text=piece)
        for heading, body in split_sections(markdown)
        for piece in window(body, size, overlap)
    ]
