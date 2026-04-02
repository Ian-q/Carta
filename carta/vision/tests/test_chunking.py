"""Tests for structured chunking module."""
import pytest
from carta.vision.chunking import (
    Chunk,
    is_markdown_table,
    extract_tables,
    chunk_structured_text,
    chunk_extraction_result,
    merge_small_chunks,
    chunk_ocr_output,
    _estimate_tokens,
)


class TestMarkdownTableDetection:
    """Detecting markdown tables in text."""
    
    def test_simple_table_detected(self):
        """Simple markdown table is detected."""
        table = "| Col1 | Col2 |\n|------|------|\n| A    | B    |"
        assert is_markdown_table(table) is True
    
    def test_table_with_alignment(self):
        """Table with alignment markers detected."""
        table = "| Left | Center | Right |\n|:-----|:------:|------:|\n| A    | B      | C     |"
        assert is_markdown_table(table) is True
    
    def test_not_table_single_line(self):
        """Single line is not a table."""
        text = "| Just one line |"
        assert is_markdown_table(text) is False
    
    def test_not_table_plain_text(self):
        """Plain text without pipes is not a table."""
        text = "This is just regular text.\nNo table here."
        assert is_markdown_table(text) is False
    
    def test_not_table_single_row(self):
        """Single row with pipes but no separator is not a table."""
        text = "| Header 1 | Header 2 |"
        assert is_markdown_table(text) is False
    
    def test_multiline_cell_table(self):
        """Table with multiline content detected."""
        table = "| Header |\n|--------|\n| Line1\\nLine2 |"
        # This has separator, so should be detected
        assert is_markdown_table(table) is True


class TestTableExtraction:
    """Extracting tables from text."""
    
    def test_extract_single_table(self):
        """Extract one table from text."""
        text = "Intro text\n\n| Col1 | Col2 |\n|------|------|\n| A | B |\n\nOutro text"
        parts = extract_tables(text)
        
        assert len(parts) == 2  # (intro, table), (outro, "")
        assert "Intro text" in parts[0][0]
        assert "| Col1 | Col2 |" in parts[0][1]
        assert "Outro text" in parts[1][0]
    
    def test_extract_multiple_tables(self):
        """Extract multiple tables from text."""
        text = """Text before.

| Table1 | Col |
|--------|-----|
| A      | B   |

Middle text.

| Table2 | Col |
|--------|-----|
| C      | D   |

Text after."""
        parts = extract_tables(text)
        
        # Should have at least 2 parts with tables
        tables_found = [p for p in parts if p[1]]  # parts with non-empty tables
        assert len(tables_found) >= 2
        assert any("Table1" in p[1] for p in tables_found)
        assert any("Table2" in p[1] for p in tables_found)
    
    def test_no_tables_returns_whole_text(self):
        """Text without tables returns as single part."""
        text = "Just plain text.\nNo tables here."
        parts = extract_tables(text)
        
        assert len(parts) == 1
        assert parts[0][0] == text
        assert parts[0][1] == ""
    
    def test_table_at_start(self):
        """Table at beginning of text."""
        text = "| H1 | H2 |\n|----|----|\n| A | B |\n\nFollowing text."
        parts = extract_tables(text)
        
        assert len(parts) == 2
        assert parts[0][0] == ""  # No text before table
        assert "H1" in parts[0][1]


class TestChunkStructuredText:
    """Chunking structured text with table preservation."""
    
    def test_table_preserved_as_single_chunk(self):
        """Tables are never split."""
        text = "| Header |\n|--------|\n" + "| Row |\n" * 100  # Long table
        chunks = chunk_structured_text(text, max_tokens=50)  # Small limit
        
        # Should have exactly 1 chunk (the table)
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "table"
        # Table should be intact
        assert "| Header |" in chunks[0].text
    
    def test_text_and_table_separate_chunks(self):
        """Text and tables become separate chunks."""
        text = """Introduction paragraph.

| Col1 | Col2 |
|------|------|
| A    | B    |

Conclusion text."""
        chunks = chunk_structured_text(text, max_tokens=100)
        
        # Should be: intro chunk, table chunk, conclusion chunk
        assert len(chunks) == 3
        assert chunks[0].chunk_type == "text"
        assert chunks[1].chunk_type == "table"
        assert chunks[2].chunk_type == "text"
    
    def test_large_text_chunks_respect_limit(self):
        """Large text is chunked within token limits."""
        # Create text larger than max_tokens with paragraphs to enable splitting
        # Each "Word " is ~1.3 tokens (1 word * 1.3)
        # Need multiple paragraphs to trigger splitting
        paragraphs = ["Word " * 50 for _ in range(10)]  # 10 paragraphs of ~65 tokens each
        text = "\n\n".join(paragraphs)  # Total ~650 tokens
        chunks = chunk_structured_text(text, max_tokens=200)
        
        # Should be multiple chunks due to paragraph breaks
        assert len(chunks) >= 2
        # All chunks should be within limit
        for chunk in chunks:
            if chunk.chunk_type == "text":
                # Allow 50% margin for estimation variance
                assert chunk.estimated_tokens <= 300, f"Chunk too large: {chunk.estimated_tokens} tokens"
    
    def test_overlap_preserves_context(self):
        """Overlapping chunks share content."""
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_structured_text(text, max_tokens=50, overlap_fraction=0.2)
        
        # With overlap, adjacent chunks should share some words
        if len(chunks) > 1:
            chunk1_words = set(chunks[0].text.lower().split())
            chunk2_words = set(chunks[1].text.lower().split())
            # Should have some overlap
            overlap = chunk1_words & chunk2_words
            # At least a few words should overlap
            assert len(overlap) > 0
    
    def test_chunk_metadata_populated(self):
        """Chunks have correct metadata."""
        text = "| H |\n|---|\n| V |"
        chunks = chunk_structured_text(
            text,
            max_tokens=100,
            page_num=5,
            section_heading="Section A"
        )
        
        assert len(chunks) == 1
        assert chunks[0].page_num == 5
        assert chunks[0].section_heading == "Section A"
        assert chunks[0].chunk_type == "table"
        assert chunks[0].estimated_tokens > 0


class TestExtractionResultChunking:
    """Chunking based on extraction content type."""
    
    def test_text_content_uses_structured_chunking(self):
        """TEXT content uses table-preserving chunking."""
        text = "| H |\n|---|\n| V |"
        chunks = chunk_extraction_result(
            text,
            content_type="text",
            has_tables=True,
            page_num=1
        )
        
        # Should detect and preserve table
        table_chunks = [c for c in chunks if c.chunk_type == "table"]
        assert len(table_chunks) == 1
    
    def test_visual_content_simple_chunking(self):
        """VISUAL content uses simple chunking."""
        text = "Just some text.\nNo tables here."
        chunks = chunk_extraction_result(
            text,
            content_type="visual",
            has_tables=False,
            page_num=2
        )
        
        # All chunks should be text type
        for chunk in chunks:
            assert chunk.chunk_type == "text"
    
    def test_mixed_content_structured_chunking(self):
        """MIXED content uses structured chunking."""
        text = "| H |\n|---|\n| V |"
        chunks = chunk_extraction_result(
            text,
            content_type="mixed",
            has_tables=False,  # Even without explicit flag
            page_num=3
        )
        
        # Should still detect table
        table_chunks = [c for c in chunks if c.chunk_type == "table"]
        assert len(table_chunks) == 1


class TestMergeSmallChunks:
    """Merging small chunks."""
    
    def test_small_text_chunks_merged(self):
        """Small text chunks are merged together."""
        chunks = [
            Chunk(text="Short", chunk_type="text", estimated_tokens=10),
            Chunk(text="Also short", chunk_type="text", estimated_tokens=15),
            Chunk(text="Tiny", chunk_type="text", estimated_tokens=5),
        ]
        merged = merge_small_chunks(chunks, min_tokens=50)
        
        # All small chunks should be merged into one
        assert len(merged) == 1
        assert "Short" in merged[0].text
        assert "Also short" in merged[0].text
        assert "Tiny" in merged[0].text
    
    def test_tables_not_merged(self):
        """Tables are never merged with other content."""
        chunks = [
            Chunk(text="| H |\n|---|\n| V |", chunk_type="table", estimated_tokens=10),
            Chunk(text="Small text", chunk_type="text", estimated_tokens=10),
        ]
        merged = merge_small_chunks(chunks, min_tokens=50)
        
        # Should have 2 chunks: table separate, text pending
        assert len(merged) == 2
        assert merged[0].chunk_type == "table"
        assert merged[1].chunk_type == "text"
    
    def test_large_chunks_preserved(self):
        """Large chunks are preserved standalone."""
        chunks = [
            Chunk(text="Large chunk with many words here", chunk_type="text", estimated_tokens=100),
            Chunk(text="Small", chunk_type="text", estimated_tokens=5),
        ]
        merged = merge_small_chunks(chunks, min_tokens=50)
        
        # Large chunk preserved, small one merged into it
        assert len(merged) == 1
        assert "Large chunk" in merged[0].text
        assert "Small" in merged[0].text
    
    def test_empty_list_returns_empty(self):
        """Empty list returns empty."""
        merged = merge_small_chunks([], min_tokens=50)
        assert merged == []


class TestOcrOutputChunking:
    """Convenience function chunk_ocr_output."""
    
    def test_returns_dict_format(self):
        """Returns chunks as dicts."""
        text = "| H |\n|---|\n| V |"
        chunks = chunk_ocr_output(text, max_tokens=100, page_num=3)
        
        assert len(chunks) == 1
        assert "text" in chunks[0]
        assert "chunk_type" in chunks[0]
        assert "page_num" in chunks[0]
        assert chunks[0]["page_num"] == 3
        assert chunks[0]["chunk_type"] == "table"
    
    def test_dict_has_all_fields(self):
        """Dicts have all expected fields."""
        text = "Some text"
        chunks = chunk_ocr_output(text, max_tokens=50)
        
        for chunk in chunks:
            assert "text" in chunk
            assert "chunk_type" in chunk
            assert "page_num" in chunk
            assert "section_heading" in chunk
            assert "estimated_tokens" in chunk


class TestTokenEstimation:
    """Token estimation."""
    
    def test_empty_text(self):
        """Empty text returns 1."""
        assert _estimate_tokens("") == 1
    
    def test_short_text(self):
        """Short text returns reasonable estimate."""
        tokens = _estimate_tokens("Hello world")
        assert tokens >= 1
        # "Hello world" = 2 words * 1.3 = 2.6, or 11 chars / 3 = 3.67
        assert 2 < tokens < 5
    
    def test_longer_text_higher_estimate(self):
        """Longer text has higher estimate."""
        short = _estimate_tokens("Hello")
        long = _estimate_tokens("Hello world this is longer")
        assert long > short


class TestChunkDataclass:
    """Chunk dataclass behavior."""
    
    def test_chunk_creation(self):
        """Can create Chunk with all fields."""
        chunk = Chunk(
            text="Test content",
            chunk_type="text",
            page_num=5,
            section_heading="Section",
            estimated_tokens=10
        )
        assert chunk.text == "Test content"
        assert chunk.chunk_type == "text"
        assert chunk.page_num == 5
    
    def test_chunk_defaults(self):
        """Chunk has sensible defaults."""
        chunk = Chunk(text="Test", chunk_type="table")
        assert chunk.page_num == 0
        assert chunk.section_heading == ""
        assert chunk.estimated_tokens == 0


class TestEdgeCases:
    """Edge cases and error handling."""
    
    def test_whitespace_only_text(self):
        """Whitespace-only text handled gracefully."""
        chunks = chunk_structured_text("   \n\n   ", max_tokens=100)
        # Should handle gracefully, possibly return empty or minimal chunks
        assert isinstance(chunks, list)
    
    def test_very_long_table(self):
        """Very long table still preserved as single chunk."""
        # Table larger than max_tokens
        rows = "| Row | Data |\n" * 500
        table = f"| H1 | H2 |\n|----|----|\n{rows}"
        chunks = chunk_structured_text(table, max_tokens=50)
        
        # Should still be one table chunk
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "table"
    
    def test_table_with_no_separator_not_detected(self):
        """Table-like text without separator not detected."""
        text = "| Not | A | Table |\n| No | Separator | Here |"
        # This looks like table rows but no separator line
        # Actually, extract_tables regex requires separator
        chunks = chunk_structured_text(text, max_tokens=100)
        
        # Should be treated as text (not a valid table)
        for chunk in chunks:
            assert chunk.chunk_type == "text"
