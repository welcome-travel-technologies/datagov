"""Run luccy's unchanged RAG app live on DataGov's Qdrant.

luccy (welcome-rag-new, image `luccy:local`) normally reads an index bundle from disk: page
files, `lexical.bin`, `kw_stem.npz`, `emb_gemini.npy`, `sig.txt`, `catalog.jsonl`. This
package serves every one of those reads from the Qdrant that DataGov's Document Explorer
engine already fills (collection `document_explorer_v1`, one point per chunk, produced by
luccy's own `retrieve.chunk_body` and embedded with luccy's own embedder):

    sync.py   `python -m luccy_qdrant.sync`: DataGov's corpus export (contract C2) -> a
              `luccy` payload on the in-scope chunk points + a keyword collection
              `luccy_kw_<stamp>` (luccy's own Qdrant/bm25 sparse vectors, same point ids)
              behind the alias `luccy_kw`. No file is written and nothing is embedded.
    live.py   the read side: luccy's chunk list, keyword scorer, meaning search, page bodies
              (rebuilt with the engine's shared `document_explorer.stitch.stitch_document`)
              and catalog lookups, answered from Qdrant with bounded LRU caches only.
    hooks.py  the explicit list of luccy attributes replaced or wrapped before luccy's web
              app is imported, each pinned to the luccy source it was written against.
    serve.py  `python -m luccy_qdrant.serve`: luccy's own gunicorn command on `app.py`,
              which installs the hooks and then imports luccy's app unchanged.

STORAGE_MAP.md lists every storage read luccy makes and how each one is served.
"""
