# Changelog

All notable changes to **carta-cc** are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.2] — 2026-04-05

### Fixed
- Synced `plugin.json` and `marketplace.json` versions to match package version (were stuck at `0.3.0`).
- Replaced `${user_config.*}` template variables in dev `.mcp.json` with hardcoded localhost defaults (substitution syntax only works in plugin manifests, not hand-written project `.mcp.json` files).
- Removed stale `UserPromptSubmit`/`Stop` hook registrations from `.claude/settings.json` that pointed to missing `.carta/hooks/` scripts; plugin-native `hooks/hooks.json` handles these.

### Changed
- README: removed Superpowers dependency from plugin install instructions; replaced with direct marketplace install (`extraKnownMarketplaces` → `Ian-q/Carta`).
- README: reorganised install section — plugin path is primary, pip/uvx/curl unified under "CLI install".
- README: fixed curl URL (`carta-cc/carta-cc` → `Ian-q/Carta`).
- `skills/carta-init/SKILL.md`: corrected Step 3 verification — `carta init` copies hook scripts to `.carta/hooks/` but does not write `.claude/settings.json` (plugin-native handles Claude Code hook registration).

## [0.3.1] — 2026-04-04

### Fixed
- Fixed PyPI publish failure caused by version mismatch (tag v0.3.1 vs package 0.3.0).
- Fixed CI workflow `claude plugin validate` command with correct path argument.
- Fixed `plugin.json` schema to include required `type` and `title` fields for userConfig.

## [0.3.0] — 2026-04-04

### Added
- **Native Claude Plugin Architecture** 
  - Integrated `.mcp.json` support.
  - Added auto-registration hooks in `hooks/hooks.json`.
  - Removed legacy cache and shifted to canonical `.claude-plugin/plugin.json`.
- **Multimodal Visual Search**
  - Added Visual Embeddings (ColPali / ColQwen2) using native transformers API.
  - Expanded search scope to include visual collections directly from Qdrant.
  - Implemented structured PDF chunking with dual-extraction and table preservation.
- **Bootstrapping Enhancements**
  - Added graceful degradation when Qdrant is unreachable during `carta init`.
  - Added auto-creation of `AGENTS.md` and default slash commands.

### Changed
- Reorganized test suite (moved `conftest.py` out of `carta/` to repo root).
- Migrated away from deprecated `colpali-engine` API.

## [0.2.0] — 2026-04-01

### Added
- **Collection scoping module** (`carta/search/scoped.py`) for multi-project search
  - `get_search_collections(cfg, scope)` with `repo`/`shared`/`global` scope levels
  - `discover_collections(qdrant_url)` - discovers Carta collections from Qdrant
  - `filter_by_permission()` - project filtering with `include`/`exclude`/`all` modes
  - Global collections support (`carta_global_*` collections)
- **Multi-platform MCP support**
  - `.mcp.json` for Claude Code MCP registration
  - `.opencode.json` for OpenCode MCP registration
  - Both created automatically during `carta init`
- **Scoped search in MCP tool**
  - `carta_search(query, top_k, scope)` with default `scope="repo"`
  - Searches across multiple collections and merges results
  - Secure default: only current project collections
- **Lifecycle tracking in sidecars**
  - `current_path` field for hash-based drift detection
  - `file_hash` and `file_mtime` fields
  - Generation tracking for stale document detection
  - `status` field: `embedded` | `stale` | `orphaned`
- **Vision model pipeline for PDFs**
  - PyMuPDF-based image extraction from PDF pages
  - Ollama vision model integration (LLaVA/moondream2)
  - Automatic image description generation and embedding
  - Sidecar tracking with `image_chunks` and `vision_status`
- **Document lifecycle management**
  - `mark_stale()` - marks documents as stale when content changes
  - `cleanup_orphans()` - removes orphaned chunks from deleted documents
  - Healed sidecars automatically during embed operations
- **Smart hook v0.2** (Phase 3)
  - Automatic context injection on UserPromptSubmit
  - Three-zone score routing (high >0.85, gray 0.60-0.85, low <0.60)
  - Ollama judge for gray-zone queries (3s timeout, fail-open)

### Changed
- **MCP-first architecture** - `.mcp.json` is sole registration point
  - Removed v0.1.x plugin cache system
  - Added automatic cleanup of old plugin cache on `carta init`
- **Bootstrap hardened**
  - Post-deletion assertion for plugin cache cleanup
  - Parent-glob aware .gitignore updates
  - Portable `exec` quoting for hooks
- **Updated command structure**
  - `carta-hook` registered as console script
  - Shell stubs use exec delegation pattern

### Removed
- **Plugin cache system** (v0.1.x compatibility)
  - `~/.claude/plugins/carta/` directory no longer used
  - `~/.claude/plugins/cache/carta-cc/` directory no longer used
  - Automatic cleanup on `carta init`

### Fixed
- **stdout pollution in MCP server** - all logging now goes to stderr
- **sys.exit in MCP server** - returns structured errors instead
- **Hook fail-open logic** - returns True (inject) on timeout, not False
- **Collection naming** - consistent `{project}_{type}` namespacing
- **PDF embedding** - batch upserts with per-file timeout

[0.2.0]: https://github.com/Ian-q/Carta/compare/v0.1.11...v0.2.0

## [0.1.11] — 2026-03-24

### Added

- **`docs/install.md`** — single source for pipx, venv `PATH`, `--pip-args` syntax, PlatformIO conflicts, and post-install smoke checks.
- **Embed concurrency lock** (`.carta/embed.lock`) with atomic create and stale-PID cleanup.
- **Qdrant preflight** and **per-file progress** for `carta embed`; clearer **`carta search`** messages (empty index vs Qdrant/query errors).
- **Homeless-doc** defaults: root-file whitelist (e.g. `CHANGELOG.md`, `AGENTS.md`) and anchor path basename matching.
- **Skill cache**: warnings when replacing stale plugin metadata or removing old version directories; **PlatformIO** PATH note when another `carta` exists on `PATH`.

### Fixed

- **`carta scan`** now passes `.carta/scan-results.json` into the scanner so **`changed_since_last_audit`** baselines match the file the CLI uses.
- **`run_embed`** returns structured errors on Qdrant failure (no `sys.exit` inside the pipeline).
- **`upsert_chunks`** uses a bounded Qdrant client timeout when no client is passed in.

### Docs

- **README** links to `docs/install.md`.
- **Install test guide** defers install details to `docs/install.md` and updates first-run / baseline notes.

[0.1.11]: https://github.com/Ian-q/Carta/compare/v0.1.10...v0.1.11
