"""PDF text extraction and section-aware chunking for carta embed."""

import re
import yaml
from pathlib import Path

from carta.mupdf_util import mupdf_quiet


def extract_pdf_text(pdf_path: Path) -> list[dict]:
    """Extract text from a PDF, returning a list of page dicts.

    Each dict: {"page": int, "text": str, "headings": list[str]}
    Headings are detected via font-size heuristic (>= 13pt).
    """
    import fitz  # pymupdf

    with mupdf_quiet():
        doc = fitz.open(str(pdf_path))
        pages = []

        for page_num, page in enumerate(doc, start=1):
            blocks = page.get_text("dict")["blocks"]
            text_parts = []
            headings = []

            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_text = ""
                    max_size = 0
                    for span in line.get("spans", []):
                        line_text += span["text"]
                        max_size = max(max_size, span["size"])
                    line_text = line_text.strip()
                    if not line_text:
                        continue
                    text_parts.append(line_text)
                    if max_size >= 13:
                        headings.append(line_text)

            pages.append({
                "page": page_num,
                "text": "\n".join(text_parts),
                "headings": headings,
            })

        doc.close()
    return pages


def _strip_frontmatter(text: str) -> tuple[str, dict]:
    """Strip YAML frontmatter from markdown text.

    Args:
        text: Raw markdown text content.

    Returns:
        Tuple of (text_without_frontmatter, frontmatter_dict).
        frontmatter_dict is empty if no frontmatter detected.
    """
    match = re.match(r'^---\n(.*?\n)---\n', text, re.DOTALL)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        remaining = text[match.end():]
        return remaining, meta
    return text, {}


def extract_markdown_text(md_path: Path) -> tuple[list[dict], dict]:
    """Extract text from a Markdown file, splitting on heading boundaries.

    Returns a tuple of (sections, frontmatter_meta) where sections is a list of
    dicts with the same shape as extract_pdf_text: {"page": int, "text": str, "headings": list[str]}.

    Args:
        md_path: Path to the .md file.

    Returns:
        Tuple of (sections, frontmatter_meta dict).
    """
    text = md_path.read_text(encoding="utf-8")
    text, frontmatter_meta = _strip_frontmatter(text)

    # Split on ## or ### heading boundaries; keep the delimiter with each section
    raw_sections = re.split(r'(?=^#{2,3}\s)', text, flags=re.MULTILINE)

    sections = []
    for i, section in enumerate(raw_sections):
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        first_line = lines[0].strip() if lines else ""

        # Extract heading from first line if it starts with ##
        if first_line.startswith("#"):
            heading = first_line
            body = "\n".join(lines[1:]).strip()
        else:
            heading = "(intro)"
            body = section

        # Skip empty sections (whitespace-only body and no meaningful content)
        if not body and heading == "(intro)":
            continue
        if not body and not first_line:
            continue
        # Skip if combined content is whitespace only
        combined = (heading + " " + body).strip()
        if not combined or combined == heading.strip() and not body:
            # Only skip pure heading with no body text
            if not body:
                continue

        sections.append({
            "page": i + 1,
            "text": section,
            "headings": [heading],
        })

    return sections, frontmatter_meta


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. Uses max of word-count and char-count heuristics
    to avoid underestimating punctuation-heavy content (e.g. TOC dot leaders).

    Uses len/3 (not /4) for the char estimate — technical content (hex tables,
    register maps, dot leaders) tokenises at ~3 chars/token, not ~4.  This keeps
    chunks safely under nomic-embed-text's 2048-token context limit.
    """
    word_estimate = len(text.split()) * 1.3
    char_estimate = len(text) / 3
    return max(1, int(max(word_estimate, char_estimate)))


def chunk_text(
    pages: list[dict],
    max_tokens: int = 400,
    overlap_fraction: float = 0.15,
) -> list[dict]:
    """Chunk extracted PDF pages into sized chunks with overlap.

    Returns list of dicts:
        {"chunk_index": int, "page": int, "section_heading": str, "text": str}
    """
    chunks = []
    chunk_index = 0
    overlap_words = int(max_tokens * overlap_fraction / 1.3)

    for page_data in pages:
        page_num = page_data["page"]
        text = page_data["text"]
        headings = page_data.get("headings", [])
        current_heading = headings[0] if headings else ""

        if _estimate_tokens(text) <= max_tokens:
            chunks.append({
                "chunk_index": chunk_index,
                "page": page_num,
                "section_heading": current_heading,
                "text": text,
            })
            chunk_index += 1
            continue

        paragraphs = re.split(r"\n\n+", text)
        current_chunk_words: list[str] = []
        current_chunk_heading = current_heading

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            for h in headings:
                if para.startswith(h):
                    current_heading = h
                    break

            para_words = para.split()

            if _estimate_tokens(" ".join(current_chunk_words + para_words)) <= max_tokens:
                if not current_chunk_words:
                    current_chunk_heading = current_heading
                current_chunk_words.extend(para_words)
            else:
                if current_chunk_words:
                    chunks.append({
                        "chunk_index": chunk_index,
                        "page": page_num,
                        "section_heading": current_chunk_heading,
                        "text": " ".join(current_chunk_words),
                    })
                    chunk_index += 1

                    overlap_carry = current_chunk_words[-overlap_words:] if overlap_words > 0 else []
                    current_chunk_words = list(overlap_carry) + para_words
                    current_chunk_heading = current_heading
                else:
                    original_words_len = len(para_words)
                    safety_iters = 0
                    while para_words:
                        safety_iters += 1
                        if safety_iters > max(10, original_words_len * 2):
                            raise RuntimeError(
                                "chunk_text stalled while splitting an oversized paragraph "
                                f"(page={page_num}, max_tokens={max_tokens}, overlap_words={overlap_words})."
                            )
                        take = []
                        while para_words and _estimate_tokens(" ".join(take + [para_words[0]])) <= max_tokens:
                            take.append(para_words.pop(0))
                        if not take:
                            take.append(para_words.pop(0))
                        chunks.append({
                            "chunk_index": chunk_index,
                            "page": page_num,
                            "section_heading": current_heading,
                            "text": " ".join(take),
                        })
                        chunk_index += 1
                        if para_words:
                            if overlap_words > 0 and len(take) > 1:
                                overlap_cap = max(0, len(take) // 4)
                                overlap_len = min(overlap_words, overlap_cap)
                                overlap = take[-overlap_len:] if overlap_len > 0 else []
                            else:
                                overlap = []
                            para_words = overlap + para_words
                    current_chunk_words = []
                    current_chunk_heading = current_heading

        if current_chunk_words:
            chunks.append({
                "chunk_index": chunk_index,
                "page": page_num,
                "section_heading": current_chunk_heading,
                "text": " ".join(current_chunk_words),
            })
            chunk_index += 1

    return chunks


def chunk_transcript(
    transcript_text: str,
    max_tokens: int = 400,
) -> list[dict]:
    """Chunk a transcript into sized chunks at speaker/segment boundaries.

    Returns list of dicts: {"chunk_index": int, "text": str}
    """
    speaker_pattern = re.compile(r"(?=^[A-Z][A-Za-z0-9 ]+:)", re.MULTILINE)
    segments = speaker_pattern.split(transcript_text)
    segments = [s.strip() for s in segments if s.strip()]

    chunks = []
    chunk_index = 0
    current_words: list[str] = []

    for segment in segments:
        segment_words = segment.split()

        if _estimate_tokens(" ".join(current_words + segment_words)) <= max_tokens:
            current_words.extend(segment_words)
        else:
            if current_words:
                chunks.append({
                    "chunk_index": chunk_index,
                    "text": " ".join(current_words),
                })
                chunk_index += 1
                current_words = list(segment_words)
            else:
                while segment_words:
                    take = []
                    while segment_words and _estimate_tokens(" ".join(take + [segment_words[0]])) <= max_tokens:
                        take.append(segment_words.pop(0))
                    if not take:
                        take.append(segment_words.pop(0))
                    chunks.append({
                        "chunk_index": chunk_index,
                        "text": " ".join(take),
                    })
                    chunk_index += 1

    if current_words:
        chunks.append({
            "chunk_index": chunk_index,
            "text": " ".join(current_words),
        })

    return chunks
