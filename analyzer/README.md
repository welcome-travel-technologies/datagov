# Luccy service for DataGov

This folder contains the shared Luccy service used by the independent **Luccy (Beta)**
app and the DataGov chat integration. The chat integration is disabled for users
while `LUCCY_CHAT_ENABLED=False`.

- `Dockerfile` builds on the original Luccy serving image.
- `luccy_qdrant/` adapts its storage reads to DataGov's existing Qdrant chunks and
  vectors. It reconstructs whole documents from chunk ordinals without page files
  or a second embedding of the corpus.
- The original answering algorithm lives in the private
  [`welcome-travel-technologies/luccy`](https://github.com/welcome-travel-technologies/luccy)
  repository. The adapter checks its source pins before serving.
- The independent app is luccy's own chat page (`LUCCY_DEV_UI=1` on `analyzer`), shown by
  `frontend/app/document-analyzer/` through DataGov's sign-in gate
  (`backend/app/document_analyzer/page_gate.py`).
- The backend status endpoint, page gate and chat tool are in `backend/app/document_analyzer/`.
- Private comparison testing runs through `scripts/compare-luccy.ps1` and
  `luccy_qdrant/compare/`. There is no comparison frontend or browser results API.

Changes to this service can affect both consumers. See
[`docs/document-analyzer.md`](../docs/document-analyzer.md) for setup and tests.
