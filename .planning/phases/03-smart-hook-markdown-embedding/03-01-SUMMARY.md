---
phase: 03-smart-hook-markdown-embedding
plan: 01
subsystem: embed
tags: [markdown, embedding, config, pipeline, qdrant, ollama, parse]

requires:
  - phase: 02-mcp-tools
    provides: MCP server + pipeline service layer that this extends with markdown support

provides:
  - Updated proactive_recall DEFAULTS with three-zone thresholds (high/low/judge_timeout_s)
  - extract_markdown_text() in parse.py with heading-aware splitting and frontmatter stripping
  - Pipeline dispatch for .md files in pipeline.py
  - file_type field in sidecar stubs (induct.py)

affects:
  - 03-02 (smart hook module — depends on config thresholds)
  - 03-03 (integration — markdown files now embeddable)

tech-stack:
  added: []
  patterns:
    - "Heading-aware markdown splitting: re.split on ##/### boundaries before overlap chunker"
    - "Frontmatter stripping: re.match ^---...--- before section split; keys stored as Qdrant metadata"
    - "Extension-based dispatch: file_path.suffix == '.md' branch in _embed_one_file"

key-files:
  created: []
  modified:
    - carta/config.py
    - carta/embed/parse.py
    - carta/embed/pipeline.py
    - carta/embed/induct.py
    - carta/embed/tests/test_embed.py
    - carta/tests/test_config.py

key-decisions:
  - "Use qwen2.5:0.5b (not phi3.5-mini) per REQUIREMENTS.md — smaller model fits <=2B constraint"
  - "extract_markdown_text returns (list[dict], dict) tuple — same page/text/headings shape as extract_pdf_text"
  - "frontmatter_meta stored as metadata['frontmatter'] key in Qdrant payload when present"
  - "file_type field added to sidecar stub to distinguish markdown vs pdf documents"

patterns-established:
  - "Markdown sections map to 'pages' for chunker compatibility: page=i+1, text=full section, headings=[heading_line]"
  - "Empty sections (whitespace-only body) skipped before building sections list"

requirements-completed: [HOOK-07, EMBED-01]

duration: 20min
completed: 2026-03-26
---

# Phase 03 Plan 01: Config Thresholds + Markdown Embedding Summary

**Three-zone hook thresholds in config DEFAULTS and full markdown embed pipeline via heading-aware extractor with YAML frontmatter stripping**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-03-26T03:30:00Z
- **Completed:** 2026-03-26T03:50:00Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Replaced `similarity_threshold`/`ollama_judge` in DEFAULTS with `high_threshold` (0.85), `low_threshold` (0.60), `judge_timeout_s` (3), updated `max_results` to 5, updated model to `qwen2.5:0.5b`
- Added `_strip_frontmatter()` and `extract_markdown_text()` to `parse.py` — returns same shape as `extract_pdf_text` so existing chunker works unchanged
- Pipeline dispatches `.md` files to markdown extractor; frontmatter keys stored in Qdrant payload as `frontmatter` dict
- Sidecar stubs now include `file_type: markdown` or `file_type: pdf`
- 7 new tests added across `test_embed.py` and `test_config.py`; all pass

## Task Commits

1. **Task 1: Config schema + markdown extractor** - `8894171` (feat, TDD)
2. **Task 2: Pipeline markdown dispatch + sidecar file_type** - `3aa9245` (feat)

## Files Created/Modified

- `carta/config.py` - Updated `proactive_recall` DEFAULTS with three-zone thresholds
- `carta/embed/parse.py` - Added `import yaml`, `_strip_frontmatter()`, `extract_markdown_text()`
- `carta/embed/pipeline.py` - Added `.md` to `_SUPPORTED_EXTENSIONS`, imported `extract_markdown_text`, added extension dispatch in `_embed_one_file`
- `carta/embed/induct.py` - Added `file_type` field to `generate_sidecar_stub`
- `carta/embed/tests/test_embed.py` - Added 6 new tests (markdown extractor, sidecar file_type, extensions)
- `carta/tests/test_config.py` - Added `test_proactive_recall_defaults`

## Decisions Made

- Used `qwen2.5:0.5b` over `phi3.5-mini` per REQUIREMENTS.md (fits <=2B model constraint better)
- `extract_markdown_text` returns a `(sections, frontmatter_meta)` tuple; sections list matches `extract_pdf_text` shape so `chunk_text` requires no changes
- Frontmatter metadata stored as `metadata["frontmatter"]` in Qdrant payload only when non-empty

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

Pre-existing test failure in `carta/mcp/tests/test_server.py::test_server_main_is_callable` due to `mcp` module not installed in this Python environment — unrelated to this plan's changes. 83 tests pass; 1 pre-existing failure.

## Next Phase Readiness

- Config thresholds (`high_threshold`, `low_threshold`, `judge_timeout_s`) ready for hook module (03-02)
- Markdown files now embeddable via `carta embed` — `EMBED-01` complete
- No blockers

---
*Phase: 03-smart-hook-markdown-embedding*
*Completed: 2026-03-26*
