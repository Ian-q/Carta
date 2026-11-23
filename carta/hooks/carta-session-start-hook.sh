#!/usr/bin/env bash
# carta-session-start-hook.sh — SessionStart hook: print the session brief.
#
# `carta sessions hook session-start` prints the recent sessions of this worktree,
# then of this repo (at most 15 lines, from stored payloads, no model call), and
# prints nothing at all when Carta is not set up here, session memory is off, or
# Qdrant is unreachable. Always exits 0.
command -v carta >/dev/null 2>&1 || exit 0
carta sessions hook session-start 2>/dev/null || true
exit 0
