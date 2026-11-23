#!/usr/bin/env bash
# carta-stop-hook.sh — Stop and SessionEnd hook: index the session that just ended.
#
# `carta sessions hook stop` reads the hook's JSON from stdin (transcript_path, cwd,
# session_id, hook_event_name), and when the project has modules.session_memory on,
# starts `carta sessions index --session <transcript>` as a detached background
# process and returns at once. It never blocks the session, never prints, and exits
# 0 on every error: with Qdrant or Ollama down the background run fails quietly
# (log: <carta home>/sessions/<project>/hook-index.log) and the transcript is
# picked up by the next run. On Stop, a session is re-indexed at most once per
# sessions.stop_hook_min_interval_s; SessionEnd always indexes.
command -v carta >/dev/null 2>&1 || exit 0
carta sessions hook stop >/dev/null 2>&1 || true
exit 0
