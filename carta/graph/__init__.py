"""Carta's second arm: a deterministic graph beside the embedding arm.

Edges are extracted from doc text with no model, stored in one SQLite file
under ``.carta/graph/``, and queried in reverse ("which docs describe this
file?"). Nothing in this package imports the vector stack; it works with
Qdrant and Ollama unreachable.
"""
