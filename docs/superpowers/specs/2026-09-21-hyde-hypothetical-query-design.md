# HyDE: Caller-Written Hypothetical Answers for Search

**Date:** 2026-09-21
**Status:** Approved under delegation (Ian handed the session over for unattended work: "the Carta
improvements up to you"). The data below is the case; review before merging.
**Motivating question:** can a model-generated *answer* find documents that the *question* cannot?

## Case study — why this exists

Ian's framing: it is hard to ask the right question to get the right passage, especially when the
answer is buried in a 400-page PDF and phrased nothing like the question. His proposed mechanism was
to have a model write a deliberately *wrong* answer and search for the chunk "opposite" to it.

That mechanism cannot work as stated. Dense embeddings encode **topic, not truth**: "the bus runs at
250 kbps" and "the bus runs at 500 kbps" embed almost identically, so a wrong answer lands next to
the right one, and the vector opposite to it is simply off-topic. But the diagnosis under it is
real and has a name — **query/document asymmetry**: a short question and a long passage do not live
in the same region of embedding space. The known remedy is **HyDE** (Hypothetical Document
Embeddings, Gao et al. 2022): have a model write a *plausible* answer, embed that, and search with
it. The fabricated answer's facts do not matter; its vocabulary and shape do.

Both ideas — the negative anchor and HyDE — were measured rather than argued.

## Measurement

**Eval set.** The 62-query ET-embed set scored recall@5 0.984 (saturated) and was lost from disk in
July. It was rebuilt as an 84-query set (#19) designed not to saturate: 25 `pdf-deep` (answer buried
in a ≥40-chunk PDF), 52 `vocab-mismatch` (no distinctive words shared with the answer), 33 `why`
(design rationale), 61 with `reject` hard negatives, 18 `lookup` controls. Built by 4 writer agents +
4 adversarial verifiers over the *indexed* text, machine-checked (every evidence quote grounded in
the gold file's indexed chunks, every path substring precise), then TREC-style pooled-judged: every
non-gold doc any variant ranked above the gold (1,040 pairs) was blind-judged; one `expect` was added
and two ambiguous queries dropped. It lives in the ET-embed repo, not here (company content).

**Harness.** The real search code — `run_search` (CLI, hybrid BM25+dense) and `carta_search` (MCP,
dense-only) — with only the dense query vector swapped per variant. BM25 always sees the raw query.
k=10; paired bootstrap 95% CI on ΔMRR vs baseline; "+/-" = queries whose rank improved / worsened.

**Hypothetical writers.** `qwen3.5:9b` (homelab, self-hosted); Claude with a one-line project
description and no tools ("bare"); Claude with the project's CLAUDE.md in context ("ctx" — what a
Claude session calling Carta actually has). CLAUDE.md is not an indexed doc, so it cannot leak a gold
passage. Writers never saw the corpus (verified from their transcripts).

| variant (dense query vector) | CLI MRR | CLI ΔMRR 95% CI | MCP MRR | MCP ΔMRR 95% CI | MCP recall@5 |
|---|---:|---|---:|---|---:|
| baseline `embed(query)` | 0.554 | — | 0.463 | — | 0.536 |
| **Claude-ctx, mean(query, hyp)** | 0.650 | **[+0.036, +0.158]** 27/5 | 0.698 | **[+0.155, +0.319]** 36/3 | 0.774 |
| Claude-ctx, hyp only (doc prefix) | 0.673 | [+0.050, +0.187] 31/7 | 0.694 | [+0.137, +0.329] 36/9 | 0.810 |
| Claude-ctx, hyp only (query prefix) | 0.686 | [+0.060, +0.204] 33/7 | 0.726 | [+0.164, +0.363] 36/7 | 0.821 |
| Claude-bare, mean(query, hyp) | 0.648 | [+0.034, +0.155] 25/5 | 0.651 | [+0.114, +0.263] 32/4 | 0.750 |
| 9b, mean(query, hyp) | 0.592 | [−0.013, +0.090] 21/9 | 0.512 | [−0.022, +0.120] 23/14 | 0.619 |
| 9b, hyp only (doc prefix) | 0.480 | [−0.144, −0.004] 15/23 | 0.419 | [−0.142, +0.054] 20/27 | 0.512 |
| wrong answer used as HyDE | 0.497 | [−0.113, −0.007] 13/17 | 0.407 | [−0.134, +0.019] 22/24 | 0.524 |
| negative anchor `q − 0.3·wrong` | 0.531 | [−0.060, +0.012] 9/10 | 0.421 | [−0.086, −0.004] 9/18 | 0.500 |
| negative anchor `q − 0.6·wrong` | 0.494 | [−0.119, −0.003] 11/21 | 0.372 | [−0.160, −0.025] 9/26 | 0.440 |
| "opposite of the wrong answer" `−wrong` | 0.307 | [−0.320, −0.177] 5/43 | 0.000 | [−0.563, −0.365] 0/55 | 0.000 |

Per-tag (MCP, MRR): Claude-ctx mean lifts `vocab-mismatch` 0.32→0.63, `pdf-deep` 0.42→0.66, `why`
0.41→0.66, `hard-negative` 0.45→0.73; `lookup` (the control) 0.82→0.94. On the CLI hybrid path the
same lifts are smaller (`vocab-mismatch` 0.43→0.55, `pdf-deep` 0.48→0.55) — BM25 already recovers
part of what the dense lane misses.

## Conclusions

1. **HyDE works when the caller writes the hypothetical.** Claude-written hypotheticals are a large,
   statistically clear win on both paths, biggest on the dense-only MCP path (+0.24 MRR, recall@5
   0.54→0.77). CLAUDE.md context helps on top.
2. **A local model is not a good enough writer.** `qwen3.5:9b`'s hypotheticals drift; only the
   `mean` blend avoided harm, and its CI crosses zero on both paths. Carta should not generate
   hypotheticals itself — which is fortunate: the caller that benefits (Claude, via MCP or the
   `/doc-search` skill) is already a frontier model and writes one for free, with zero added latency
   and nothing new running locally. The local-only constraint is untouched.
3. **The negative anchor is refuted.** Subtracting a wrong answer is neutral at best and harmful at
   larger weights; searching for its opposite finds nothing (0/84 on the dense-only path — BM25 is
   all that rescues it on the hybrid path). Using a wrong answer *as* a probe also hurts.
4. **Ship `mean`, not the single best-scoring variant.** The three Claude variants are within noise
   of each other, but `mean` has the fewest regressions (with CLAUDE.md context: 5 and 3 vs 7–9 for
   the other two; without it: tied on the CLI path, 4 vs 9–10 on MCP), and of the three 9b variants
   it is the only one whose CI does not fall below zero. Production hypotheticals will vary in quality;
   keeping the question in the vector bounds the downside when a hypothetical is off-topic.

## Design

- **Blend:** `dense_vec = unit(unit(embed("search_query: " + query)) + unit(embed("search_document: "
  + hypothetical)))`. The hypothetical is embedded as a *document* — it is shaped like one.
- **Lanes:** only the dense lane changes. BM25 keeps the raw query (a hypothetical's invented numbers
  and identifiers would pollute lexical matching); the ColPali visual lane keeps the raw query.
- **Surfaces:**
  - MCP `carta_search(query, top_k, scope, hypothetical=None)` — the tool description tells the
    agent what to write (2–5 sentences phrased as the project's docs would state the answer; it need
    not be correct). This path has no BM25 lane, so the blend moves its whole text ranking.
  - CLI `carta search … --hypothetical TEXT`, and `run_search(…, hypothetical=None)`.
  - `/doc-search` skill: write a hypothetical and pass it.
- **Not on the hook.** The proactive-recall hook has no frontier caller to write one, and a local
  writer does not help (conclusion 2).
- **Failure:** a blank hypothetical is ignored. The hypothetical is embedded on the same backend as
  the query, so an outage already fails the query embed first; no new failure mode.
- **Default off by construction:** nothing changes unless a caller passes a hypothetical.

## Testing

Unit (TDD): the blend math; `run_search` sends the blended vector to the dense lane and the raw query
to BM25; no hypothetical → byte-identical behaviour; blank → ignored; doc prefix on the
hypothetical; MCP passes it through; CLI flag wiring. End-to-end verification: re-running the 84
queries through the *shipped* `run_search`/`carta_search` with the Claude-ctx hypotheticals must
reproduce the benchmark's `mean` numbers exactly (retrieval is deterministic).

## Rejected alternatives

- **Carta generates hypotheticals with local Ollama** (`search.hyde.model`) — measured not to help
  with a 9b writer; would add seconds of latency and a model dependency to every search.
- **Negative anchor / "opposite of wrong"** — refuted above.
- **Hypothetical into the BM25 lane** — not measured; fabricated specifics are exactly what a
  lexical lane must not match on.
