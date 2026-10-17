"""Hard bounds on untrusted input.

Doc text, file names and config values come from the repository being read,
so nothing here is configurable: a limit a repository could raise would not
be a limit. Every bound is checked before the work it protects. They are far
above anything a hand-written doc reaches; an input beyond one is skipped (and
counted where there is a report), never an error.
"""
from __future__ import annotations

MAX_DOC_BYTES = 2 * 1024 * 1024         # a larger doc is not read at all
MAX_LINE_LENGTH = 8192                  # a longer line is not scanned (minified or encoded data)
MAX_CANDIDATES_PER_DOC = 50_000         # stage 1 stops emitting after this many
MAX_LINK_TARGET_LENGTH = 512            # a longer "](target)" is not a link to a repo path
MAX_RAW_CANDIDATE_LENGTH = 1024         # rejected before any normalisation is attempted
MAX_WRAPPER_LAYERS = 8                  # "((<`x`>))": layers of wrapping stripped at most
MAX_GLOBS_PER_DOC = 256                 # distinct globs one doc may have counted over the path set
MAX_GLOB_LENGTH = 1024                  # a longer glob matches nothing
MAX_SUFFIX_DEPTH = 32                   # a deeper path is never found by its suffix

MAX_CONFIG_BYTES = 1024 * 1024          # .carta/config.yaml, as read by the replay
MAX_CONFIG_GLOBS = 256                  # entries in doc_globs / ignore_docs / excluded_paths
MAX_PATTERNS = 64                       # graph.patterns entries
MAX_PATTERN_REGEX_LENGTH = 512

MAX_CACHE_PAYLOAD_BYTES = 16 * 1024 * 1024      # one decompressed replay-cache entry
MAX_CACHED_TOKENS = 200_000                     # distinct tokens in one cached doc

# -- the edge store, read back --------------------------------------------------
# The store sits in the repository's own ``.carta/``, so a repository can ship
# one. What is read back from it is held to what Carta writes.
MAX_STORE_BYTES = 1024 * 1024 * 1024    # a larger store (with its journal) is not opened
MAX_STORED_TEXT_LENGTH = 4096           # a path, target, key or evidence in one row
MAX_STORED_EDGE_META_LENGTH = 64 * 1024  # one edge's meta JSON

# -- the replay -----------------------------------------------------------------
MAX_REPLAY_COMMIT_FILES = 5000          # upper bound of graph.replay.max_files (the bulk rule)
MAX_REPLAY_TEXT_BYTES = 64 * 1024 * 1024    # doc text held at once while one commit is ranked

# -- numbers a config may set -----------------------------------------------------
# Each is a count or a size somewhere downstream, so each has a ceiling here.
MAX_IMPACT_TOP = 1000                   # graph.impact.top
MAX_IMPACT_WEIGHT = 1000.0              # graph.impact.weights.*
MAX_IDENT_MIN_LENGTH = 64               # graph.identifiers.min_length
MAX_SCAN_HISTORY_LIMIT = 100_000        # graph.scan.history_limit (commits git is asked for)
MAX_REPLAY_WINDOW = 1_000_000           # graph.replay.popularity_window and .min_population

# -- co-change (#130) ----------------------------------------------------------
MAX_COCHANGE_COMMIT_FILES = 500         # upper bound of graph.cochange.max_files (pairs are n^2)
MAX_COCHANGE_PATH_LENGTH = 1024         # a longer path is never counted
MAX_EXTERNAL_GRAPH_BYTES = 64 * 1024 * 1024     # an adjacency file given to the holdout eval
MAX_EXTERNAL_GRAPH_EDGES = 2_000_000   # rows of that file, accepted or not
MAX_COCHANGE_PAIRS = 2_000_000          # pairs one table holds; past it no new pair is started
MAX_GIT_TOKEN_BYTES = 64 * 1024         # one NUL-terminated record of a streamed git answer
MAX_REPLAY_HISTORY_CHANGES = 5_000_000  # path changes the replay holds for one history

# -- the symbol arm (#131) ------------------------------------------------------
MAX_SYMBOLS_FILE_BYTES = 64 * 1024 * 1024       # a larger symbols file is not read
MAX_SYMBOL_LINE_BYTES = 16 * 1024               # a longer index line is skipped unparsed
MAX_SYMBOL_ROWS = 2_000_000                     # more rows than this is a failed load
MAX_SYMBOL_NAME_LENGTH = 200                    # a longer name (or scope) is not a row
MAX_DEFINITIONS_PER_NAME = 64                   # kept per name; more only means "ambiguous"
MAX_SYMBOL_DEFINITIONS_SETTING = 16             # ceiling of graph.symbols.max_definitions
MAX_CTAGS_FILES = 200_000                       # files handed to one ctags run
MAX_CTAGS_FILE_BYTES = 2 * 1024 * 1024          # a larger source file is not indexed
MAX_CTAGS_OUTPUT_BYTES = 512 * 1024 * 1024      # ctags is stopped beyond this much output
CTAGS_TIMEOUT_SECONDS = 600                     # ...and after this long
CTAGS_PROBE_TIMEOUT_SECONDS = 10                # the capability probe
MAX_CODE_SCAN_FILE_BYTES = 32 * 1024 * 1024     # a larger file is data, not code (word scan)
MAX_CODE_SCAN_BYTES = 1024 * 1024 * 1024        # total read by one word scan
MAX_SYMBOL_HISTORY_CHECKS = 1000                # git lookups one scan may spend on danglers
