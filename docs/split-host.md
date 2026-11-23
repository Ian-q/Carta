# Split-host deployment

Carta's slow work is all on the ingest side: OCR runs at roughly a minute and a half per page on
a laptop CPU, and vision descriptions and a large judge model are not much better. A spare
machine with a GPU does that work in seconds. The search path is different. The recall hook runs
on every prompt and blocks it, so it needs a host that answers in milliseconds and is always
reachable.

A split-host setup puts each kind of work on the machine that suits it:

| Work | Where | Config key |
|---|---|---|
| Vector store | GPU host, or any always-on box | `qdrant_url` |
| Document embeds, OCR, vision (`carta embed`) | GPU host | `embed.ingest_ollama_url` |
| Query embed, HyDE, LLM rerank, recall-hook judge, stale-scan judge | this machine | `embed.ollama_url` |

The hostnames below are placeholders.

## Configure

```yaml
# .carta/config.yaml
qdrant_url: http://gpu-box.example:6333

embed:
  ollama_url: http://localhost:11434               # query path: keep it local
  ingest_ollama_url: http://gpu-box.example:11434  # ingest path: the GPU host
  ollama_model: nomic-embed-text:latest            # must be the same build on both hosts

proactive_recall:
  connect_timeout_s: 1.0   # TCP probe of remote backends before each prompt
  search_timeout_s: 3      # wall-clock budget for the recall search
```

If you leave `ingest_ollama_url` out, both paths use `ollama_url`, exactly as before this setting
existed. Single-host projects need no change.

Pull the models each host uses:

```bash
# this machine (query path)
ollama pull nomic-embed-text
# gpu-box (ingest path)
ollama pull nomic-embed-text
ollama pull glm-ocr
ollama pull qwen3-vl:8b
```

The embedding model has to be the same build on both machines. Documents are embedded on the
GPU host and queries on this machine. Two different builds put their vectors in different
spaces, and search quality drops with no error message. `carta doctor` compares the two digests.

Ollama on the GPU host has to listen on the network (`OLLAMA_HOST=0.0.0.0`), and Qdrant's port
has to be reachable from this machine. Neither service has authentication by default. Keep them
on a private network or a VPN, and do not expose either one to the internet.

## Verify

```bash
carta doctor
```

Phase 5 reports each endpoint as local or remote, with its round-trip time, and then checks:

- that Qdrant is reachable. A remote Qdrant is never "fixed" by starting a local Docker one.
- that the query host has the embedding model. Without it, search fails.
- that the ingest host has the embedding model (an error if missing), plus the OCR and vision
  models (a warning if missing). An unreachable ingest host is only a warning, because search
  keeps working without it.
- that the embedding model is the same build on both hosts.
- for a remote endpoint, whether one round trip uses more than a third of
  `proactive_recall.search_timeout_s` (a warning).

`carta hook explain "some prompt"` shows what the hook would do for one prompt, including any
backend it could not reach.

## When the remote host is away

Before doing any work, the recall hook opens one TCP connection to each **non-local** backend
(Qdrant and the query Ollama), in parallel, under `proactive_recall.connect_timeout_s`. If one
does not answer, for example because the VPN is down or the host is asleep, the hook prints one
line to stderr and lets the prompt through without recall:

```
carta-hook: Qdrant at http://gpu-box.example:6333 unreachable (connect timed out after 1s) — skipping recall (fail-open)
```

That costs at most `connect_timeout_s` per prompt, whatever the network does. A hung DNS lookup
is covered too. Local endpoints are not probed, because a local service that is down refuses the
connection at once. Set `connect_timeout_s: 0` to turn the probe off.

The whole hook also has a hard ceiling. By default it is the sum of its budgets (connect, search,
judge, plus 4 s to shorten prompts over 500 characters) plus 1 s. Set
`proactive_recall.hook_timeout_s` to override it. If any step overruns its own timeout, the hook
still exits at that ceiling.

`carta embed` against an unreachable ingest host fails the same way it does when a local Ollama
is stopped. Run it again once the host is back.
