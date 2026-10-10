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
