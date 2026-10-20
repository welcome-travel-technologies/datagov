# luccy storage map: every read, and how it is served live from DataGov's Qdrant

luccy 0.19.1 (`welcome-rag-new` c0f50f3, image `luccy:local`) reads a corpus from disk: page
files, an index bundle and a catalog. This adapter serves each of those reads from the Qdrant
that DataGov's Document Explorer engine fills, without changing a file in the image.

**What is in Qdrant.**
- `document_explorer_v1` is DataGov's existing collection. It holds one point per chunk, made
  by luccy's own `retrieve.chunk_body` and embedded with luccy's embedder (gemini-embedding-2,
  768 dimensions, cosine). Its payload is `{id, organization_id, document_id, version_id,
  title, page, text, index_text, section, source_type, source_url, ordinal}`.
- The sync adds one payload block, `luccy = {id, source, modified, title, space, url}`, to
  every chunk point of an exported current version (the organization's collections every
  member may read).
- `luccy_kw_<UTC stamp>` (behind the alias `luccy_kw`) uses the same point ids. Each point has
  luccy's Qdrant/bm25 sparse vector `bm25` (raw values, no IDF modifier) and the payload
  `{row, version_id, document_id, luccy}`. Its collection metadata holds the bundle:
  signature, chunk count N, page count, recency range, organization, build and refresh time,
  the export digest, and the exported versions that had no chunk points at the build
  (`missing_versions`).
- `luccy_kwdf_<same stamp>` holds one point per term, with the point id set to the term id and
  the payload `{n}`: the number of chunks holding that term (luccy's n_t).

**What is in memory.** Only bounded LRU caches: chunks by row, row ids, term counts, page
bodies (bounded by characters) and page records. Each one has a `LUCCY_LIVE_*` size
(settings.py).

**What is on disk.** Only luccy's operational log folder `SERVING_LOG_DIR` (default
`/app/serving/logs`). It holds answer records (`/feedback` and `/records` look them up, and
the spend check sums them), chats, `conversations.json` (page memory and titles), feedback,
notes, page errors and the embed-spend ledger. None of this is document data. Mount a named
volume there. No page, index or catalog file exists, and `/data` is not mounted. The tests
check that `retrieve.PAGES` and `retrieve.INDEX` do not exist.

**Who writes Qdrant: two keys.** The web process (`serve` → `app` → `hooks` + `live`) only
reads: aliases, collection info, count, scroll, retrieve, query. It runs with
`LUCCY_QDRANT_API_KEY`, which may be Qdrant's read-only key
(`QDRANT__SERVICE__READ_ONLY_API_KEY`). luccy's own serving code makes no Qdrant write either:
its only Qdrant calls while serving are `bundle_problem` (H12), `replacement_collection`
(H13), the `/health` check (H14) and `QdrantVectors` (never built: H1). Every write belongs
to the sync job and uses `LUCCY_QDRANT_SYNC_API_KEY` (read-write), or `LUCCY_QDRANT_API_KEY`
when that is unset (one key for both). Both are read like luccy's secrets (env, `NAME_FILE`,
`/run/secrets/<name>`).

| sync write (sync.py, sync key only) | collection |
|---|---|
| `create_payload_index` `luccy.source`, `luccy.id` | `document_explorer_v1` (non-destructive) |
| `batch_update_points` (set the `luccy` block), `delete_payload` (`luccy.source`, `luccy.id`, then `luccy`, off versions no longer exported: Qdrant 1.19.1 keeps the nested index entries when only `luccy` is deleted, so the filters would still match the point; tested, and entries left that way by an earlier sync are healed by the next) | `document_explorer_v1` |
| `create_collection`, `create_payload_index`, `upsert`, `update_collection` (metadata: bundle, `refreshed_at`) | `luccy_kw_*`, `luccy_kwdf_*` |
| `update_collection_aliases` (`luccy_kw`), `delete_collection` (prune; cleanup after a failed build) | `luccy_kw*` |

A sync handed the read-only key is refused at its first write, before anything changed, and
exits 1 with `Qdrant refused a write (403 Forbidden): the sync's Qdrant key is read-only. Set
LUCCY_QDRANT_SYNC_API_KEY ...`; a wrong key gives the 401 equivalent. The proof
(`tests/test_keys.py`, and the whole suite in two-key mode, see `tests/conftest.py`) runs
against `qdrant/qdrant:v1.19.1` started with both `QDRANT__SERVICE__API_KEY` and
`QDRANT__SERVICE__READ_ONLY_API_KEY`: the equality battery and luccy's whole web app (e2e:
start-up, `/health`, `/ask`, the stream, the viewer, feedback, bundle pickup) serve on the
read-only key, a write spy records no Qdrant write outside `sync.py`, and Qdrant refuses the
serving client a write (403).

Hooks are the entries of `hooks.HOOKS`. Each one is pinned: its luccy source must hash to
`pins.py`, or the image does not build (a difference at run time is a loud warning). They
are numbered H1 to H14 below.

## 1. retrieve.py: the retriever, the bundle and the keyword leg

| luccy read | what luccy needs from it | live answer |
|---|---|---|
| `Retriever.__init__`: `bundle_identity()` taken first | which bundle this process serves | **H1** replaces `__init__`. `self.bundle` is (alias target, signature) of `luccy_kw` |
| `enforce_index_health` / `index_health()` read `INDEX/REFRESH_STATE.json` | refuse a half-built bundle; `index_built` for the footer | Not called. A bundle exists only after a complete sync: the alias moves last. `index_built` = the bundle's `refreshed_at`, which a no-change sync also stamps; **H4** refreshes it on the loaded retriever |
| `read_lexical(sig)` reads `INDEX/lexical.bin` (all chunk dicts plus the BM25 state); fallback `load_chunks()` reads `pages/*.md` and dedupes | the chunk list `self.chunks[row]` and `len()` | `LiveChunks`: indexable by row and sized N. A row maps to a point through the `row` payload; the chunk text comes from the chunk point. A chunk dict has luccy's exact keys and values (`page_id = luccy.id`, `title`, `url`, `space`, `tier=""`, `modified`, `breadcrumb=""`, `source_version/as_of=""`, `supersedes/superseded_by=[]`, `stale_hint=False`, `heading = section`, `text`, `index_text`). LRU cache; the rows a search returned are fetched in one call. A loop over the whole list raises (see H8 and H9) |
| `corpus_signature(chunks)` and `sig.txt` | `self.sig`: rerank cache key, run records `corpus_sig` | Computed by the sync with luccy's own formula, in luccy's row order. It is equal to the file index's signature on the same corpus (tested) |
| `build_vectors` / `np.load(emb_gemini.npy, mmap)` | `self.emb` (flat search only) | `None`. Meaning search is always in Qdrant. No embedding call is made: the vectors are DataGov's |
| `BM25(...)` over every chunk (`LUCCY_KW_STEM=0`) | in-memory BM25 | Not supported; the pins refuse `LUCCY_KW_STEM=0`. luccy's default is the stemming leg |
| `KwStem(INDEX/kw_stem.npz)`, `.scores(query)` | float32 score per row: Σ f32(ln((N−n+0.5)/(n+0.5)+1))·value | `LiveKwStem`: the same `stem_encoder()` query terms. n_t comes from `luccy_kwdf_*` and N from the bundle. The query goes to `luccy_kw_*` with luccy's idf as term weights; the result widens until no unreturned chunk can reach the 20th score; the candidates are rescored bit-exact with luccy's float32 arithmetic (`kwscore.py`). Every other row is 0, and luccy keeps only rows above 0 |
| `Retriever.retrieve`: keyword → mask → meaning → union → rerank → per-page cap → bundle | toggles masked before both legs take their top candidates | **H2** wraps luccy's own `retrieve`, unchanged. It hands the toggles to the keyword leg, which filters `luccy.source` inside Qdrant. luccy passes them to the meaning leg itself |
| `Retriever._source_keep` (`source_of` over every chunk) | per-row mask | **H3**: all True. Both legs already excluded the switched-off sources, and luccy reads the mask only at rows they returned |
| `QdrantVectors(len, sig)` finds the collection holding the bundle; `search(qv, k, off)` → rows | top-k rows by dot product, minus toggled-off sources | `LiveVectors.search`: `document_explorer_v1` filtered to the organization and `luccy.source ∈ sources left on` (luccy's `exact` setting honoured). Cosine on DataGov's unit vectors ranks as luccy's dot does. Hit ids → rows through `luccy_kw_*`; a point outside this bundle is skipped |
| `mods = [c["modified"] …]` → `_min_mod/_max_mod` | recency bonus range | From the bundle metadata (the sync computes min/max over the synced pages) |
| `superseded.json` and `dedupe_pages()` (breadcrumb siblings) | supersession and copy dropping at index time | No-op: DataGov keeps no breadcrumb or ancestors, so luccy would group nothing either. `superseded_by` is empty, so the superseded demotion never fires |
| `_int8_reranker_dir` writes `INDEX/rerank_int8` | int8 reranker | Keep `RERANK_INT8=0` (the image default) |

## 2. ingest/qdrant_index.py and serving/jobs.py: bundle checks and restarts

| luccy read | live answer |
|---|---|
| `jobs._load_models` → `qdrant_index.bundle_problem(client, INDEX)` (reads `sig.txt` and `gemini_hashes.txt`) | **H12**: checks the alias, the keyword, statistics and chunk collections. Before the first sync the reason is `luccy_kw alias missing: run analyzer-sync (python -m luccy_qdrant.sync)`; luccy's retry loop loads once a sync has run |
| `jobs._bundle_moved` → `retrieve.bundle_identity()` (index link folder + `sig.txt`), then `qdrant_index.replacement_collection(vs)` | **H4** `bundle_identity` = (alias target, signature). A sync that moves the alias changes it, and luccy's own pickup drains the worker and SIGTERMs it; gunicorn starts a fresh worker on the new bundle. A Qdrant error repeats the last identity, so a blip never restarts. **H13** `replacement_collection` → None (the alias is the only pointer) |
| `QdrantVectors.search` refuses a collection holding another bundle | A bundle is immutable. The sync keeps the previous one (rollback = move the alias back), so a worker still on it keeps answering while it restarts |

## 3. serving/rqb.py: page bodies, whole pages and ticket lines

| luccy read | live answer |
|---|---|
| `page_meta(id)` reads `pages/<id>.md` (frontmatter + body); `page_body` calls it | **H5**: page fields from the bundle's `luccy` block. Body = every chunk point of that page's version, rebuilt by the engine's shared `document_explorer.stitch.stitch_document` (sorted by (page, ordinal)), then stripped as `parse_page` strips it. None when unknown or empty. Bodies are LRU-cached by characters. A chunk without `ordinal` (written before ordinals and not yet backfilled by `python -m document_explorer.ordinals backfill`) or a chunk gone from the collection gives `""`. That is luccy's own result for a page it cannot read: `fill_whole_pages` marks it `empty` and keeps its passages, and nothing raises (tested) |
| `fill_whole_pages(merged)` → `page_body(id)` per merged page, caps 20,000 per page and 240,000 total | luccy's own function, unchanged, over the stitched body (caps applied exactly as luccy applies them, to the length of what it gets) |
| `ticket_line(p)` reads the Jira page's frontmatter (issuetype, status, priority, prio, effort, stakeholder, reporter, assignee, epic) | **H6**: `""`. DataGov exports none of these fields, and luccy itself returns `""` for a page without an issue type. **Degraded**: no `ticket:` line in the evidence. DataGov's Jira body text carries `Status/Type/Updated` lines instead |
| `evidence_text`, `render`, `batch_retrieve`, `text_coverage` | Unchanged (pinned); they read only the page dicts |

## 4. serving/driver.py

| luccy read | live answer |
|---|---|
| `AnswerService.retriever()` → `rqb.Retriever(rebuild=False)` | The live retriever (H1) |
| `section_read`: loops over every chunk for the top page's heading section (`LUCCY_SECTION_READ=1`) | **H9** wraps luccy's own function. Inside, iterating the chunk list yields the top page's rows (in row order, fetched in one call) and a read-only placeholder for every other row, so `enumerate` stays true |
| `_pages_from_ids` (the "elaborate" route): `enumerate(r.chunks)` filtered by page id; rerank cache key uses the row numbers | **H8** wraps luccy's own method with the same scoped iteration (true row numbers, so the rerank cache key is luccy's) |
| `_owner_names_note` → `page_owners(id)` reads `created_by/last_updated_by` from the page file | **H7** (`ingest.redact.page_owners`, also bound as `serving.driver.page_owners`): `()`. **Degraded**: DataGov keeps no page owners, so no "Page owner:" line |
| `_deep_fallback`: `open_doc=page_body`, `find_in_doc=find_lines(page_body(id))` | Through H5 (stitched body) |
| `_glossary()` reads `LUCCY_GLOSSARY_PATH`, else the glossary shipped in the image (`serving/glossary.json`) | Unchanged. This is luccy's KPI glossary (Welcome's concepts and measures), not DataGov document data. `LUCCY_GLOSSARY_ENRICH=0` / `LUCCY_DEFINITION_ROUTE=0` turn it off |
| `config.PROMPTS/*.md`, `warehouse.registry` | Code shipped in the image, unchanged |
| `_oracle_pages` (`LUCCY_ORACLE_PASSAGES`) | Eval only; never set |

## 5. serving/deep.py

| luccy read | live answer |
|---|---|
| `jira_tickets()` reads `catalog.jsonl` rows with an issue type (`list_tickets` tool, "my tickets") | **H10**: `[]`. **Degraded**: DataGov exports no reporter, assignee, status or issue type, so `list_tickets` finds no tickets (luccy's own result for a catalog without Jira rows) |
| `search`, `open_doc`, `find_in_doc` | Through rqb (H5) and the live retriever |

## 6. serving/api: the document viewer and health

| luccy read | live answer |
|---|---|
| `page_routes._catalog_row(id)` reads `catalog.jsonl` (re-read on mtime) for title, space, url, modified; only ids in it are served | **H11**: the same four fields from the bundle's `luccy` block. Only ids in the bundle are served (404 otherwise) |
| `page_routes.document` → `page_meta(id)` for body and breadcrumb; `_answer_passages` reads the run record in `LOG_DIR` | Body through H5. Breadcrumb is empty (no ancestors on DataGov; **degraded**: the viewer shows no parent path). Run records stay in `LOG_DIR` |
| `app.health` reads `INDEX`, `sig.txt` (+ mtime), `catalog.jsonl` line count, `kw_stem.npz`, `REFRESH_STATE.json`, `gemini_hashes.txt` line count, and asks Qdrant for the bundle collection | **H14** swaps the `/health` route in place (same `HealthResponse` shape, path and order). It reports `index.dir = qdrant:<bundle>`, `sig`, `age_hours` (since refresh), `chunk_count = N`, `corpus_pages`, `refresh_last_ok`, and `qdrant = {collection, alias, keyword}`. It answers 503 with reasons when the alias, statistics or chunk collection is missing, or the models are not loaded (no point count: the sync checks the counts before it moves the alias). luccy's image `HEALTHCHECK` is unchanged |
| `/feedback`, `/notes`, `/page-errors`, `/records`, `/chats*`, `/conversation/diagnostics`, spend check, `conversations.json` | Operational logs in `SERVING_LOG_DIR` (named volume), unchanged |
| `serving/config.py`: `CATALOG = /app/catalog.jsonl` | Never read: its three readers are H10, H11 and H14 |
| `serving/config.py`: `LOG_DIR` | The operational log volume (above) |

## 7. ingest/redact.py: names

| luccy read | live answer |
|---|---|
| `load_name_dict()` reads `pages/*.md` and `.name_dict.json` | Never called while serving (`LUCCY_REDACT_NAMES=0`, luccy's default). `scrub_page_text` redacts with an empty roster |
| `page_owners` | H7 |

## 8. luccy page ids (contract C3; `ids.py` is the single source of truth)

The public API is `luccy_id_for(row)`. For a whole export, `assign(rows)` also resolves the
case where two rows claim one id. Both take C2 export rows (`external_id`, `document_id`,
`source_type`, `source_url`, `collection_name`/`group`). The sync stores `assign()`'s ids, and
the comparison runner imports the same two functions, so both sides name pages alike. For a
row whose natural id no other row claims, `assign(rows)` gives exactly `luccy_id_for(row)`,
in any row order; the id never depends on `LUCCY_LIVE_UPLOAD_URL_TEMPLATE` (tested).

| DataGov `external_id` / `source_type` | luccy id | luccy source |
|---|---|---|
| `confluence:<id>` | `<id>` | confluence (all-digit id) |
| `confluence:<id>:part:<n>` | **`<id>-part-<n>`** | confluence (needs a `/wiki/` link) |
| `jira:<KEY>` | `<KEY>` (luccy's own Jira ids are the issue key, `ingest/jira_ingest.py`) | jira |
| `upload` | **`gd-dg-<document_id>`** | drive |
| github / dbt with a github.com link | `dg-<document_id>` | github |
| powerbi | `dg-<document_id>`, space `PBI` | powerbi |
| anything whose intended source luccy would not compute (e.g. dbt without a GitHub link, a part without a `/wiki/` link) | `gd-dg-<document_id>` | drive (counted in the sync report) |

Why the two adjusted forms:

- **`-part-` replaces C3's `:part:`.** luccy reads a cited id with
  `[A-Za-z0-9][A-Za-z0-9_-]{3,}`. Both `serving/driver.py` PROSE_DOC_REF (the link render)
  and `serving/claim_ground.py` _DOC_REF (the grounding and number checks) use it. A `:` stops
  both, so `doc 6857523203:part:2` would be read as page `6857523203`, and those checks would
  silently skip the clause or link the wrong page. `-` is inside both character classes.
- **`gd-dg-` for uploads.** `retrieve.source_of` calls an id Google Drive only when it starts
  with `gd`. A `dg-` id with no GitHub or `/wiki/` link reads as **Jira**. That would put
  uploads under the Jira toggle, and luccy would ask the page for ticket fields. A `gd`
  prefix makes luccy's drive rules apply instead: the Drive toggle, and the "Google Drive"
  label in the viewer. One side effect remains: an upload with no link gets luccy's Drive
  title-search link. `LUCCY_LIVE_UPLOAD_URL_TEMPLATE` (e.g.
  `https://datagov.example/documents/{document_id}`) gives such uploads a real link instead.

Every id must also match C3's `^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}$` without `..`, luccy's
viewer `DOC_ID_RE`, and the citation pattern (4 characters at least: 1–3 digit numbers read as
evidence ranks). Otherwise it falls back as above. The link is the C2 `source_url` passed
through the engine's own `safe_source_url`, which is what the chunks carry. `space` is `PBI`
for Power BI, the project key for Jira, and otherwise the DataGov collection name: DataGov
keeps no Confluence space key.

## 9. What luccy does that DataGov's data cannot reproduce

- **Supersession and temporal resolve.** DataGov has no breadcrumb or ancestors, so
  `dedupe_pages` groups nothing, nothing is marked `superseded_by`, and `temporal.resolve`'s
  topic recency never fires. luccy behaves exactly as it does on a corpus without breadcrumbs.
  The equality test's period pair (QBR Q1 2024 / Q2 2025) is kept as two current pages in
  both modes.
- **Jira ticket line, page owners and `list_tickets`.** These need ticket people, status,
  type and epic, and page owners. DataGov's export carries none of them (H6, H7, H10).
- **Viewer breadcrumb and luccy's `tier`.** Both are empty.
- **Whole-page text is rebuilt, not stored.** The stitched body equals the original under the
  stitcher's `normalize()`. Exceptions are the engine's documented stitching limits
  (`engine tests: test_limit_*`), which keep the same words in a different place. Every
  DataGov Jira body hits one of them: its short `Status/Type/Updated` preamble moves after
  the `Description` heading. Restored headings can also differ in level (`#` vs `##`), and
  whitespace differs. luccy's whole-page caps apply to the stitched length. On the test
  corpus the length differs from the original by 4 characters at most, so a page within a
  few characters of 20,000, or a running total within a few characters of 240,000, could
  flip between whole and passages.
- **Engine chunking differences.** These apply to the data, not to the adapter.
  - Multi-page uploads (PDF) are chunked per physical page: heading context resets on every
    page, where luccy would chunk one body. The stitched body joins pages with a blank line.
  - Heading-only documents get the engine's fallback chunks, where luccy would index nothing.
  - `section` is cut at 500 characters, where luccy keeps the whole heading path.
- **Engine re-upserts wipe the `luccy` block.** DataGov's engine re-upserts a version's
  points whole, payload included, when one of them is missing. Until the next sync those
  points drop out of the meaning leg, which filters on `luccy.source`. The keyword leg
  (its own collection) and page reads (point ids) still reach them. An unchanged-digest
  sync counts the points that carry the block. It re-sets the block from the bundle
  ("repaired"), or rebuilds when the bundle's points no longer all exist. An unchanged run
  is a few requests.
- **Rename without a new version.** The digest covers the set of current versions, so a
  renamed title or collection is picked up on the next new version, or with
  `sync --force`.
- **Transition window.** Between a sync's payload update and the worker restart (at most
  `BUNDLE_CHECK_S` = 5 s plus the drain), a dense hit on a point outside the loaded bundle is
  skipped, so that query briefly has fewer than 20 meaning candidates.
- **The pickup follows the sync.** A worker still on the previous bundle keeps serving it
  until luccy's pickup notices the moved alias: at the next question or `/health` call at
  least `BUNDLE_CHECK_S` after its last look, and the question that triggers the look is
  itself still answered on the previous bundle; page reads continue through the drain.
