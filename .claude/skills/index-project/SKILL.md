---
name: index-project
description: >-
  Build or refresh a detailed, grep-optimized index of this codebase (Django
  backend + Next.js frontend) so symbol/file lookups are fast. Use when starting
  work in an unfamiliar area, before broad "where does X live?" searches, after
  large refactors/renames, or whenever the user says the index is stale. Produces
  .claude/index/{symbols.tsv,backend.md,frontend.md,INDEX.md}. Also consult the
  existing index BEFORE running wide grep/glob sweeps for a symbol or file.
---

# Index Project

A fast, structured map of this repo so you grep a ~1k-line symbol table instead
of re-scanning ~300 source files. The index covers the Django backend
(`backend/app/`) and the Next.js frontend (`frontend/`).

## When to use this skill

- **Before** a broad code search ("where is the lineage logic?", "which view
  serves `/api/items`?", "what component renders the dashboard?"). Search the
  index first — it's one file, not the whole tree.
- When **starting work in an unfamiliar area** and you need the lay of the land.
- After a **large refactor, rename, or new feature** — rebuild so the index
  matches reality.
- When the user **asks to index the project** or says search/the index is slow
  or stale.

## What it produces (`.claude/index/`)

| File | Use it for |
|---|---|
| `symbols.tsv` | **Primary lookup.** Flat `name⇥kind⇥area⇥location⇥extra`, one symbol per line. Grep this first. |
| `backend.md` | Django layer grouped by role: API endpoints, models, viewsets, serializers, tasks, ETL, management commands. |
| `frontend.md` | Next.js layer grouped by role: routes/pages, components, hooks, lib helpers, types. |
| `INDEX.md` | Overview: stats, kind/area counts, directory map, how-to-search, pointers to `docs/`. |
| `manifest.json` | Provenance (`commit`, counts) used for staleness checks. |

`kind` values: `model`, `view`, `serializer`, `endpoint`, `function`, `task`,
`mgmt-command`, `route`, `component`, `hook`, `interface`, `type`, `enum`,
`middleware`, `manager`, `class`, `const`.

`area` values: `backend:catalog`, `backend:etl`, `backend:config`,
`backend:mgmt`, `backend:tests`, `frontend:routes`, `frontend:components`,
`frontend:lib`, plus a few `*:other`.

## How to build / refresh the index

Run the bundled indexer from the repo root (no dependencies, Python 3.8+):

```bash
python .claude/skills/index-project/build_index.py
```

It rewrites every file under `.claude/index/` and stamps `manifest.json` with the
current git commit. Takes a second or two.

Check whether the existing index is stale before relying on it:

```bash
python .claude/skills/index-project/build_index.py --check
```

Prints `FRESH` (exit 0) or `STALE: …` (exit 1) by comparing `manifest.json`
against `HEAD` and the working tree. If stale, rebuild before searching.

## How to USE the index to search fast

1. **Locate a symbol** — grep the flat table, not the codebase:
   ```bash
   grep -i "ItemViewSet"           .claude/index/symbols.tsv   # by name (substring)
   grep -P "\tmodel\t"             .claude/index/symbols.tsv   # all Django models
   grep -P "\tendpoint\t"          .claude/index/symbols.tsv   # all API endpoints
   grep -P "\tcomponent\t.*lineage" .claude/index/symbols.tsv  # components in a feature
   grep -P "\tbackend:etl\t"       .claude/index/symbols.tsv   # everything in one area
   ```
   Each row ends in `path:line` — jump straight there. (On this machine prefer the
   Grep tool over shell `grep`; point it at `.claude/index/symbols.tsv`.)
2. **Browse a layer** — open `backend.md` or `frontend.md`; symbols are grouped by
   role with file headers, so you can scan an entire subsystem at a glance.
3. **Then open the source** at the `path:line` the index gave you — instead of
   running a wide tree-wide search.

Use the index for *where things are*. For *why they work that way*, read the
hand-written docs in `docs/` (architecture, api, database, etl, lineage,
assistant, governance, frontend, local-development).

## Keeping it accurate

- The index reflects a point in time (`manifest.json.commit`). It is **not**
  auto-updated — rebuild after meaningful changes, or `--check` first when unsure.
- It is a derived cache. By default it lives under `.claude/index/` and is local;
  commit it if the team wants a shared map, otherwise add `.claude/index/` to
  `.gitignore`.
- Extraction is regex-based and deterministic: top-level Python classes/functions,
  `urls.py` routes & router registrations, Next.js `app/**` routes, and TS/TSX
  `export`s. Migrations and `node_modules`/`.next`/build output are excluded by
  design. To change what's captured, edit the rules at the top of
  `build_index.py` (`AREA_RULES`, `EXCLUDE_*`, the `RE_*` patterns).
