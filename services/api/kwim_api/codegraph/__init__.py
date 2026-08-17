"""Code graph extractor - parses repos into the kwim_<team>_code graph.

Ports the patterns of DeusData/codebase-memory-mcp (MIT) into Python, Python-first:
tree-sitter structure extraction + a confidence-scored call-resolution cascade,
Louvain communities, and xxhash incremental re-indexing.

The graph holds structure/signatures/summaries/embeddings, never file bodies.
"""
