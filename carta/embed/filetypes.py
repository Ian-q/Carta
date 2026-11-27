"""The one authoritative list of source file types Carta knows about (#97).

Every consumer that asks "is this file something Carta embeds / hashes as
binary / checks for induction" derives its set from here, instead of keeping
its own literal list. Independent lists drifted three times (spreadsheets were
added to the pipeline and found missing from the scanner, then from audit).

Suffixes are lowercase with the leading dot; compare against
``path.suffix.lower()`` — discovery is case-insensitive, so ``.PDF`` counts.

Stdlib only and import-free on purpose: the scanner, audit and the
proactive-recall hook path all import it.
"""

MARKDOWN_SUFFIXES = frozenset({".md"})
PDF_SUFFIXES = frozenset({".pdf"})

# Spreadsheet sources embed text-bearing cells only. Their sidecar filename
# preserves the source extension (see carta/embed/induct.py).
SPREADSHEET_SUFFIXES = frozenset({".csv", ".xlsx"})

# Audio inputs: not embedded by the doc pipeline, but the scanner flags them
# for induction alongside the other non-markdown sources.
AUDIO_SUFFIXES = frozenset({".m4a", ".mp3", ".wav", ".aac"})

# Everything the embed pipeline extracts, chunks and embeds. Ordered: sidecar
# healing tries extensions in this order when inferring a source path.
EMBEDDABLE_SUFFIXES_ORDERED = (".pdf", ".md", ".csv", ".xlsx")
EMBEDDABLE_SUFFIXES = frozenset(EMBEDDABLE_SUFFIXES_ORDERED)

# Hashed as raw bytes (CRLF normalization would corrupt a binary container).
BINARY_SUFFIXES = frozenset({".pdf", ".xlsx"})

# What the scanner's induction / drift / LFS checks look at under the
# configured embed scan dirs: every non-markdown embeddable source, plus audio.
INDUCTION_CHECK_SUFFIXES = (EMBEDDABLE_SUFFIXES - MARKDOWN_SUFFIXES) | AUDIO_SUFFIXES

assert EMBEDDABLE_SUFFIXES == MARKDOWN_SUFFIXES | PDF_SUFFIXES | SPREADSHEET_SUFFIXES
assert BINARY_SUFFIXES <= EMBEDDABLE_SUFFIXES
