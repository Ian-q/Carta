"""Structured chunking for OCR output with table preservation.

Extends standard text chunking to handle GLM-OCR output which includes
markdown tables. Keeps table structures intact while chunking surrounding text.
"""
import re
from dataclasses import dataclass
from typing import Literal


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text.
    
    Uses character and word heuristics for rough estimation.
    
    Args:
        text: Input text
        
    Returns:
        Estimated token count
    """
    word_estimate = len(text.split()) * 1.3
    char_estimate = len(text) / 3
    return max(1, int(max(word_estimate, char_estimate)))


@dataclass
class Chunk:
    """A single chunk of content.
    
    Attributes:
        text: The chunk text content
        chunk_type: Type of content (text, table, heading, mixed)
        page_num: Source page number (if applicable)
        section_heading: Section heading this chunk belongs to
        estimated_tokens: Estimated token count
    """
    text: str
    chunk_type: Literal["text", "table", "heading", "mixed"]
    page_num: int = 0
    section_heading: str = ""
    estimated_tokens: int = 0


def is_markdown_table(text: str) -> bool:
    """Check if text is a markdown table.
    
    Detects tables formatted as:
    | Header 1 | Header 2 |
    |----------|----------|
    | Cell 1   | Cell 2   |
    
    Args:
        text: Text to check
        
    Returns:
        True if text appears to be a markdown table
    """
    lines = text.strip().split('\n')
    if len(lines) < 2:
        return False
    
    # Check for table row pattern | ... | ... |
    table_row_pattern = re.compile(r'^\|[^\n]*\|$')
    
    # At least 2 lines should match table pattern
    table_lines = sum(1 for line in lines if table_row_pattern.match(line.strip()))
    return table_lines >= 2


def extract_tables(text: str) -> list[tuple[str, str]]:
    """Extract markdown tables from text.
    
    Returns list of (preceding_text, table) tuples where table is the
    markdown table and preceding_text is the text before it.
    
    Args:
        text: Text potentially containing tables
        
    Returns:
        List of (preceding_text, table) tuples
    """
    # Pattern to match markdown tables
    # Table must have header row, separator, and at least one data row
    table_pattern = re.compile(
        r'(\|[^\n]*\|\n\|[-:\s|]+\|\n(?:\|[^\n]*\|\n?)+)',
        re.MULTILINE
    )
    
    results = []
    last_end = 0
    
    for match in table_pattern.finditer(text):
        table = match.group(1).strip()
        if is_markdown_table(table):
            preceding = text[last_end:match.start()].strip()
            results.append((preceding, table))
            last_end = match.end()
    
    # Add remaining text after last table
    remaining = text[last_end:].strip()
    if remaining:
        results.append((remaining, ""))
    
    # If no tables found, return whole text
    if not results and text.strip():
        return [(text.strip(), "")]
    
    return results


def chunk_structured_text(
    text: str,
    max_tokens: int = 800,
    overlap_fraction: float = 0.15,
    page_num: int = 0,
    section_heading: str = ""
) -> list[Chunk]:
    """Chunk text while preserving markdown tables.
    
    Strategy:
    1. Extract all markdown tables first (keep them whole)
    2. Chunk text between tables normally
    3. If a table exceeds max_tokens, keep it whole anyway (tables are critical)
    
    Args:
        text: Text to chunk (may contain markdown tables)
        max_tokens: Maximum tokens per chunk
        overlap_fraction: Fraction of chunk to overlap (0.0-1.0)
        page_num: Source page number
        section_heading: Section heading for context
        
    Returns:
        List of Chunk objects with preserved table structures
        
    Example:
        >>> text = "Some intro\\n\\n| Col1 | Col2 |\\n|------|------|\\n| A | B |\\n\\nMore text"
        >>> chunks = chunk_structured_text(text, max_tokens=100)
        >>> chunks[0].chunk_type  # "text"
        >>> chunks[1].chunk_type  # "table"
    """
    chunks = []
    overlap_tokens = int(max_tokens * overlap_fraction)
    
    # Extract tables and surrounding text
    parts = extract_tables(text)
    
    for preceding_text, table in parts:
        # Chunk the preceding text (if any)
        if preceding_text:
            text_chunks = _chunk_plain_text(
                preceding_text,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                page_num=page_num,
                section_heading=section_heading
            )
            chunks.extend(text_chunks)
        
        # Add the table as a single chunk (never split tables)
        if table:
            table_chunk = Chunk(
                text=table,
                chunk_type="table",
                page_num=page_num,
                section_heading=section_heading,
                estimated_tokens=_estimate_tokens(table)
            )
            chunks.append(table_chunk)
    
    return chunks


def _chunk_plain_text(
    text: str,
    max_tokens: int,
    overlap_tokens: int,
    page_num: int,
    section_heading: str
) -> list[Chunk]:
    """Chunk plain text without tables.
    
    Standard paragraph-based chunking with overlap.
    
    Args:
        text: Plain text to chunk
        max_tokens: Maximum tokens per chunk
        overlap_tokens: Number of tokens to overlap
        page_num: Source page number
        section_heading: Section heading
        
    Returns:
        List of text chunks
    """
    chunks = []
    
    # Split by paragraphs
    paragraphs = re.split(r'\n\n+', text)
    current_chunk_texts: list[str] = []
    current_token_count = 0
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        
        para_tokens = _estimate_tokens(para)
        
        # Check if adding this paragraph would exceed limit
        if current_token_count + para_tokens > max_tokens and current_chunk_texts:
            # Save current chunk
            chunk_text = '\n\n'.join(current_chunk_texts)
            chunks.append(Chunk(
                text=chunk_text,
                chunk_type="text",
                page_num=page_num,
                section_heading=section_heading,
                estimated_tokens=_estimate_tokens(chunk_text)
            ))
            
            # Calculate overlap
            if overlap_tokens > 0 and current_chunk_texts:
                # Take last portion for overlap
                overlap_text = chunk_text
                overlap_estimated = _estimate_tokens(overlap_text)
                while overlap_estimated > overlap_tokens and overlap_text:
                    # Truncate from beginning
                    words = overlap_text.split()
                    if len(words) > 10:
                        overlap_text = ' '.join(words[-len(words)//4:])
                    else:
                        overlap_text = ""
                    overlap_estimated = _estimate_tokens(overlap_text)
                
                current_chunk_texts = [overlap_text, para] if overlap_text else [para]
                current_token_count = _estimate_tokens(' '.join(current_chunk_texts))
            else:
                current_chunk_texts = [para]
                current_token_count = para_tokens
        else:
            current_chunk_texts.append(para)
            current_token_count += para_tokens
    
    # Don't forget the last chunk
    if current_chunk_texts:
        chunk_text = '\n\n'.join(current_chunk_texts)
        chunks.append(Chunk(
            text=chunk_text,
            chunk_type="text",
            page_num=page_num,
            section_heading=section_heading,
            estimated_tokens=_estimate_tokens(chunk_text)
        ))
    
    return chunks


def chunk_extraction_result(
    extraction_text: str,
    max_tokens: int = 800,
    overlap_fraction: float = 0.15,
    page_num: int = 0,
    content_type: str = "text",
    has_tables: bool = False
) -> list[Chunk]:
    """Chunk extracted content based on its type.
    
    Wrapper that applies appropriate chunking strategy based on content type.
    
    Args:
        extraction_text: Text extracted from PDF page
        max_tokens: Maximum tokens per chunk
        overlap_fraction: Fraction for overlap
        page_num: Source page number
        content_type: Type of content (text, visual, mixed) from extraction
        has_tables: Whether tables were detected
        
    Returns:
        List of Chunk objects
        
    Example:
        >>> from carta.vision.router import extract_pdf_with_intelligent_routing
        >>> results = extract_pdf_with_intelligent_routing(pdf_path, cfg)
        >>> for result in results:
        ...     chunks = chunk_extraction_result(
        ...         result.text,
        ...         page_num=result.page_num,
        ...         content_type=result.content_type,
        ...         has_tables=result.has_tables
        ...     )
    """
    # Always use structured chunking for OCR output (may contain tables)
    if content_type in ("text", "mixed") or has_tables:
        return chunk_structured_text(
            extraction_text,
            max_tokens=max_tokens,
            overlap_fraction=overlap_fraction,
            page_num=page_num
        )
    else:
        # Visual content - simpler chunking
        return _chunk_plain_text(
            extraction_text,
            max_tokens=max_tokens,
            overlap_tokens=int(max_tokens * overlap_fraction),
            page_num=page_num,
            section_heading=""
        )


def merge_small_chunks(chunks: list[Chunk], min_tokens: int = 50) -> list[Chunk]:
    """Merge chunks that are too small to be useful.
    
    Combines adjacent small chunks to meet minimum token threshold.
    Preserves table chunks (never merges tables with other content).
    
    Args:
        chunks: List of chunks to process
        min_tokens: Minimum tokens for a chunk to be kept standalone
        
    Returns:
        List of merged chunks
    """
    if not chunks:
        return []
    
    merged = []
    pending_texts: list[str] = []
    pending_tokens = 0
    pending_page = 0
    pending_heading = ""
    
    for chunk in chunks:
        if chunk.chunk_type == "table":
            # Flush any pending text chunks first
            if pending_texts:
                merged.append(Chunk(
                    text='\n\n'.join(pending_texts),
                    chunk_type="text",
                    page_num=pending_page,
                    section_heading=pending_heading,
                    estimated_tokens=pending_tokens
                ))
                pending_texts = []
                pending_tokens = 0
            
            # Add table as-is
            merged.append(chunk)
        else:
            # Text chunk - check size
            if chunk.estimated_tokens < min_tokens and pending_texts:
                # Merge with pending
                pending_texts.append(chunk.text)
                pending_tokens += chunk.estimated_tokens
                pending_page = chunk.page_num
                pending_heading = chunk.section_heading
            else:
                # Flush pending and start new
                if pending_texts:
                    merged.append(Chunk(
                        text='\n\n'.join(pending_texts),
                        chunk_type="text",
                        page_num=pending_page,
                        section_heading=pending_heading,
                        estimated_tokens=pending_tokens
                    ))
                
                pending_texts = [chunk.text]
                pending_tokens = chunk.estimated_tokens
                pending_page = chunk.page_num
                pending_heading = chunk.section_heading
    
    # Flush final pending
    if pending_texts:
        merged.append(Chunk(
            text='\n\n'.join(pending_texts),
            chunk_type="text",
            page_num=pending_page,
            section_heading=pending_heading,
            estimated_tokens=pending_tokens
        ))
    
    return merged


# Convenience function
def chunk_ocr_output(
    ocr_text: str,
    max_tokens: int = 800,
    overlap_fraction: float = 0.15,
    page_num: int = 0
) -> list[dict]:
    """Chunk OCR output and return as list of dicts.
    
    Convenience function that returns chunks in dict format compatible
    with the existing pipeline.
    
    Args:
        ocr_text: OCR-extracted text (may contain markdown tables)
        max_tokens: Maximum tokens per chunk
        overlap_fraction: Overlap fraction
        page_num: Page number for context
        
    Returns:
        List of chunk dicts with keys: text, chunk_type, page_num, estimated_tokens
        
    Example:
        >>> chunks = chunk_ocr_output("| Header |\\n|--------|\\n| Value |", max_tokens=100)
        >>> chunks[0]["chunk_type"]
        'table'
    """
    chunks = chunk_structured_text(
        ocr_text,
        max_tokens=max_tokens,
        overlap_fraction=overlap_fraction,
        page_num=page_num
    )
    
    # Convert to dict format
    return [
        {
            "text": chunk.text,
            "chunk_type": chunk.chunk_type,
            "page_num": chunk.page_num,
            "section_heading": chunk.section_heading,
            "estimated_tokens": chunk.estimated_tokens,
        }
        for chunk in chunks
    ]
