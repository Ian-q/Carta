# Installing Carta (carta-cc)

Carta is published on PyPI as **`carta-cc`**. The CLI command is **`carta`**.

Release history: see the repo **`CHANGELOG.md`**. After install, confirm with `carta --version`.

## Prerequisites

- **Python 3.10+**
- **pip** or **pipx** (recommended on macOS to avoid PEP 668 “externally managed environment” errors)

### Qdrant (vector store)

Carta stores embeddings in a local [Qdrant](https://qdrant.tech) vector database. Run it via Docker:

```bash
docker run -d \
  -p 6333:6333 \
  -v ~/.carta/qdrant_storage:/qdrant/storage \
  --name qdrant \
  qdrant/qdrant
```

- `-d` runs the container detached so it starts automatically with Docker.
- `-v ~/.carta/qdrant_storage:/qdrant/storage` persists your collections across container restarts and upgrades. Without this flag, all embedded documents are lost when the container stops.
- For TLS, resource limits, or upgrades, see the [Qdrant quickstart](https://qdrant.tech/documentation/quickstart/).

### Ollama (embeddings, vision, hook judge)

Install Ollama from [ollama.ai/download](https://ollama.ai/download), then pull the required models:

```bash
# Required — text embeddings (used by carta embed and carta search)
ollama pull nomic-embed-text

# Required — hook relevance judge
# Filters retrieved context before it reaches your prompt.
# Default is qwen3.5:0.8b (0.8B params, low latency).
# Set proactive_recall.ollama_model in .carta/config.yaml to swap in a larger model.
ollama pull qwen3.5:0.8b

# Optional — visual embedding (only needed for carta embed --visual)
ollama pull llava
```

### Verify

Once Qdrant and Ollama are running, confirm everything is detected:

```bash
carta doctor
```

All Phase 2 (Infrastructure) and Phase 3 (Models) checks should pass before running `carta init`.

## Recommended: pipx

```bash
pipx install carta-cc
pipx ensurepath
```

Restart the terminal (or `source ~/.zshrc` / `source ~/.bashrc`) so `carta` is on `PATH`.

Verify:

```bash
which carta
carta --version
```

## Alternative: virtualenv

```bash
python3 -m venv ~/.venv/carta
~/.venv/carta/bin/pip install carta-cc
```

Add the venv `bin` directory to `PATH` so you can run `carta` without typing the full path every time:

```bash
# Add to ~/.zshrc or ~/.bashrc (adjust the venv path if yours differs)
export PATH="$HOME/.venv/carta/bin:$PATH"
```

Restart the shell or `source` the profile, then check `which carta`.

## pipx and extra pip options

pipx forwards arguments to pip with **`--pip-args` followed by a separate token** (not `=`):

```bash
pipx install carta-cc --pip-args "--no-cache-dir"
```

Wrong: `--pip-args=--no-cache-dir` (pipx may reject this).

## PlatformIO / wrong `carta` on PATH

Some environments ship another executable named `carta` (e.g. under **`.platformio`**). If `which carta` does not point to pipx (`~/.local/bin/carta`) or your venv, put the correct bin **first** in `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

`carta init` prints a warning when it detects a mismatch.

## After install: smoke check

If `carta` is missing after a successful `pipx install`, try:

```bash
pipx reinstall carta-cc
which carta || echo "carta still missing — check pipx ensurepath and PATH"
```

## Next step

In your project: `carta init` (see the main [README](../README.md) for behaviour and configuration).
