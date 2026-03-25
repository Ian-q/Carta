# Installing Carta (carta-cc)

Carta is published on PyPI as **`carta-cc`**. The CLI command is **`carta`**.

Release history: see the repo **`CHANGELOG.md`**. After install, confirm with `carta --version`.

## Prerequisites

- **Python 3.10+**
- **pip** or **pipx** (recommended on macOS to avoid PEP 668 “externally managed environment” errors)
- Optional: **Docker** (Qdrant), **Ollama** (embeddings/search) — only if you use embed/search

If **`pipx` is not installed**: macOS `brew install pipx` then `pipx ensurepath`, or `python3 -m pip install --user pipx` and add pipx’s bin dir to `PATH`.

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
