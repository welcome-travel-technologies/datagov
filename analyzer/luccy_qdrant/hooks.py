"""The hooks: every luccy attribute this adapter replaces or wraps, and nothing else.

The pins: every luccy function a hook replaces, wraps or mirrors must still have the exact
source it was written against (sha256 of `inspect.getsource`, pins.py), plus a few constants
and signatures. The image build checks them (analyzer/Dockerfile runs
`python -m luccy_qdrant.hooks --app`), so an image whose luccy differs is never built. At
run time a pin difference only logs a loud warning; what depends on the container's own
settings (LUCCY_KW_STEM, LUCCY_VECTOR_STORE, the engine's stitcher) still refuses to start.
`install()` then swaps the attributes in memory before luccy's web app is imported (app.py).
No file in the image changes. Nothing here or in live.py writes to Qdrant: the web process
runs with a read-only key (settings.py); every write is the sync job's.

    python -m luccy_qdrant.hooks            # check the pins against the installed luccy
    python -m luccy_qdrant.hooks --app      # and install the hooks on luccy's app (the build)
    python -m luccy_qdrant.hooks --print    # the current hashes, to re-pin after a review

The table HOOKS is the authority; STORAGE_MAP.md explains each entry.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import sys
import threading
from pathlib import PurePosixPath

import numpy as np

from .live import BundleMissing, Live, LiveChunks, LiveKwStem, LiveVectors, hours_since, read_bundle
from .settings import Settings, client

# (target, kind, what the replacement serves). kind: replace | wrap | route.
HOOKS = [
    ("retrieve:Retriever.__init__", "replace",
     "chunk list (LiveChunks), keyword scorer (LiveKwStem), meaning search (LiveVectors), "
     "signature, recency range and build time from the bundle in Qdrant; no sig.txt, "
     "lexical.bin, kw_stem.npz, emb_gemini.npy, REFRESH_STATE.json or pages/"),
    ("retrieve:Retriever.retrieve", "wrap",
     "luccy's own retrieve, with the reader's source toggles handed to the keyword leg, "
     "which applies them inside Qdrant"),
    ("retrieve:Retriever._source_keep", "replace",
     "all-True mask: both legs already left the switched-off sources out in Qdrant"),
    ("retrieve:bundle_identity", "replace",
     "(alias target, signature) instead of (index link folder, sig.txt): a moved alias "
     "restarts the process exactly as a moved link does"),
    ("serving.rqb:page_meta", "replace",
     "page fields from the bundle's `luccy` block + body stitched from the chunk points"),
    ("serving.rqb:ticket_line", "replace",
     "'' : DataGov keeps no Jira ticket fields (type, status, people, epic)"),
    ("ingest.redact:page_owners", "replace",
     "() : DataGov keeps no created_by / last_updated_by (also bound as serving.driver.page_owners)"),
    ("serving.driver:AnswerService._pages_from_ids", "wrap",
     "luccy's own code; its loop over every chunk sees only the named pages' rows"),
    ("serving.driver:section_read", "wrap",
     "luccy's own code; its loop over every chunk sees only the top page's rows"),
    ("serving.deep:jira_tickets", "replace",
     "[] : no catalog.jsonl and no ticket people on DataGov, so list_tickets finds none"),
    ("serving.api.page_routes:_catalog_row", "replace",
     "display fields from the bundle's `luccy` block instead of catalog.jsonl"),
    ("ingest.qdrant_index:bundle_problem", "replace",
     "start-up check of the alias, keyword, statistics and chunk collections"),
    ("ingest.qdrant_index:replacement_collection", "replace",
     "None: the alias is the only bundle pointer (bundle_identity restarts on it)"),
    ("serving.api.app:health", "route",
     "/health from Qdrant (same HealthResponse shape) instead of sig.txt, kw_stem.npz, "
     "gemini_hashes.txt and catalog.jsonl"),
]

# Not replaced, but mirrored or relied on by the hooks: pinned all the same.
RELIED = [
    "retrieve:KwStem.scores",            # kwscore.score mirrors its float32 arithmetic
    "retrieve:write_kw_stem",            # sync encodes index_text the same way
    "retrieve:stem_encoder",
    "retrieve:corpus_signature",         # kwscore.Signature
    "retrieve:chunk_body",               # what DataGov's engine chunked with
    "retrieve:source_of",                # ids.assign
    "retrieve:parse_page",               # page_meta: stripped body, None when empty
    "retrieve:cache_key",
    "retrieve:Retriever._recency",       # reads _min_mod/_max_mod
    "retrieve:Retriever.reranker",
    "retrieve:Retriever.query_vectors",
    "serving.rqb:page_body",             # calls page_meta
    "serving.rqb:fill_whole_pages",      # calls page_body: the 20,000/240,000 caps
    "serving.rqb:batch_retrieve",
    "serving.rqb:render",
    "serving.driver:AnswerService.retriever",
    "serving.driver:AnswerService._retrieve",
    "serving.driver:AnswerService._deep_fallback",   # open_doc / find_in_doc = page_body
    "serving.deep:DeepDeps",             # default tickets = deep.jira_tickets
    "serving.api.page_routes:document",  # _catalog_row + page_meta
    "serving.jobs:_load_models",         # bundle_problem
    "serving.jobs:_bundle_moved",        # bundle_identity + replacement_collection
    "ingest.qdrant_index:QdrantVectors.search",
]

CONSTANTS = {
    "retrieve:KW_STEM_MODEL": "Qdrant/bm25",
    "retrieve:SOURCES": ("confluence", "jira", "drive", "github"),
}

SIGNATURES = {
    "retrieve:Retriever.retrieve": "(self, query: 'str', trace=False, sources: 'frozenset | None' = None, qvecs: 'dict | None' = None) -> 'dict'",
    "retrieve:Retriever.__init__": "(self, rebuild=False)",
    "serving.rqb:page_meta": "(page_id: str) -> dict | None",
    "serving.driver:section_read": "(retriever, question: str, merged: list[dict]) -> tuple[str, dict]",
}

_STATE: dict = {"installed": False, "settings": None, "live": None, "retriever": None,
                "identity": None, "orig": {}, "warnings": []}
_lock = threading.RLock()


# ------------------------------------------------------------------ pins
def resolve(target: str):
    mod, _, qual = target.partition(":")
    obj = importlib.import_module(mod)
    owner = None
    for part in qual.split("."):
        owner, obj = obj, getattr(obj, part)
    return importlib.import_module(mod), owner, qual.split(".")[-1], obj


def source_hash(obj) -> str:
    return hashlib.sha256(inspect.getsource(obj).encode()).hexdigest()[:16]


def pinned_targets(routes: bool = True) -> list[str]:
    """Every pinned target; without `routes`, none inside luccy's web app module (checking
    those imports the app, which install() must not do before the hooks are in)."""
    return [t for t, kind, _ in HOOKS if routes or kind != "route"] + RELIED


def current_pins() -> dict[str, str]:
    return {t: source_hash(resolve(t)[3]) for t in pinned_targets()}


def pin_problems(routes: bool = True) -> list[str]:
    """Every difference between luccy's internals and the pinned version."""
    from .pins import PINS
    problems = []
    for target in pinned_targets(routes):
        try:
            got = source_hash(resolve(target)[3])
        except Exception as e:  # noqa: BLE001
            problems.append(f"{target}: cannot be read ({type(e).__name__}: {e})")
            continue
        if PINS.get(target) != got:
            problems.append(f"{target}: source {got} != pinned {PINS.get(target)}")
    for target, want in CONSTANTS.items():
        try:
            got = resolve(target)[3]
        except Exception as e:  # noqa: BLE001
            problems.append(f"{target}: missing ({e})")
            continue
        if got != want:
            problems.append(f"{target}: {got!r} != {want!r}")
    for target, want in SIGNATURES.items():
        try:
            got = str(inspect.signature(resolve(target)[3]))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{target}: cannot be read ({type(e).__name__}: {e})")
            continue
        if got != want:
            problems.append(f"{target}: signature {got} != {want}")
    return problems


def environment_problems() -> list[str]:
    """What the live corpus needs from this container's settings and image."""
    import retrieve
    problems = []
    if retrieve.VECTOR_STORE != "qdrant":
        problems.append(f"LUCCY_VECTOR_STORE is {retrieve.VECTOR_STORE!r}: set LUCCY_VECTOR_STORE=qdrant")
    if not retrieve.kw_stem_on():
        problems.append("LUCCY_KW_STEM is not 1: luccy's in-memory BM25 needs every chunk in memory")
    try:
        from document_explorer.stitch import normalize, stitch_document  # noqa: F401
    except Exception as e:  # noqa: BLE001
        problems.append(f"document_explorer.stitch (the engine's shared stitcher) is not importable: "
                        f"{type(e).__name__}: {e}; the luccy image must include it")
    return problems


def warn(message: str) -> None:
    """A loud line in the container log; the process carries on."""
    _STATE["warnings"].append(message)
    print(f"[luccy_qdrant] WARNING: {message}", file=sys.stderr, flush=True)


def verify(routes: bool = True, strict: bool = True) -> None:
    """Raise when this container cannot serve the live corpus (environment_problems). Pin
    differences raise too when `strict` (the image build), else they are a loud warning."""
    problems = environment_problems()
    if problems:
        raise RuntimeError("luccy_qdrant: refusing to start:\n  " + "\n  ".join(problems))
    problems = pin_problems(routes)
    if not problems:
        return
    message = ("luccy's internals differ from the pinned version (re-review the hooks, then "
               "`python -m luccy_qdrant.hooks --print`):\n  " + "\n  ".join(problems))
    if strict:
        raise RuntimeError("luccy_qdrant: " + message)
    warn(message)


# ------------------------------------------------------------------ the live corpus
def settings() -> Settings:
    if _STATE["settings"] is None:
        _STATE["settings"] = Settings.from_env()
    return _STATE["settings"]


def current_live() -> Live:
    """The bundle this process serves: made once, on first use (after gunicorn forked)."""
    with _lock:
        if _STATE["live"] is None:
            _STATE["live"] = Live(settings())
            _STATE["identity"] = _STATE["live"].bundle.identity
        return _STATE["live"]


def reset() -> None:
    """Forget the bundle (tests; a real process restarts instead)."""
    with _lock:
        _STATE.update(live=None, retriever=None, identity=None, settings=None)


# ------------------------------------------------------------------ replacements
def live_init(self, rebuild=False):
    """retrieve.Retriever.__init__, live: the same attributes luccy's loader sets."""
    import retrieve
    from ingest import gemini_embed
    from serving import config
    if rebuild:
        raise RuntimeError("luccy_qdrant serves the live DataGov corpus; it never builds an index")
    live = current_live()
    self.bundle = live.bundle.identity                 # taken first, as luccy does
    self.index_built = live.bundle.refreshed_at or None
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    self.bm25 = None
    self.chunks = LiveChunks(live)
    self.sig = live.bundle.sig
    self.query_embedder = gemini_embed.QueryEmbedder(config.EMBED_TIMEOUT_S, config.LOG_DIR)
    self._reranker_cls = TextCrossEncoder
    self.emb = None
    self.kw_stem = LiveKwStem(live)
    self.vector_store = LiveVectors(live, exact=retrieve.VECTOR_SEARCH == "exact")
    self._reranker = None
    self._reranker_lock = threading.Lock()
    self._min_mod, self._max_mod = live.bundle.min_mod, live.bundle.max_mod
    self.live = live
    _STATE["retriever"] = self
    print(f"[luccy_qdrant] live corpus {live.bundle.name}: {live.bundle.n} chunks, "
          f"{live.bundle.pages} pages, sig {live.bundle.sig}", file=sys.stderr)


def retrieve_wrapped(self, query, trace=False, sources=None, qvecs=None):
    """luccy's Retriever.retrieve, unchanged, with the toggles given to the keyword leg."""
    import retrieve
    orig = _STATE["orig"]["retrieve:Retriever.retrieve"]
    live = getattr(self, "live", None)
    if live is None:
        return orig(self, query, trace=trace, sources=sources, qvecs=qvecs)
    off = None if sources is None else [x for x in retrieve.SOURCES if x not in sources]
    with live.toggles(off):
        return orig(self, query, trace=trace, sources=sources, qvecs=qvecs)


def source_keep(self, sources):
    """Every row kept: the live keyword search (filtered by the toggles of this thread) and
    meaning search (filtered by the `off` list luccy passes) never return a switched-off
    source, and luccy reads this mask only at rows those two returned."""
    return np.ones(len(self.chunks), dtype=bool)


def bundle_identity():
    """luccy's bundle_identity, live. A Qdrant error repeats the last identity seen, so a
    blip never restarts a process (luccy's version reads a local file)."""
    s = settings()
    try:
        live = _STATE["live"]
        qc = live.qc if live is not None else client(s)
        b = read_bundle(qc, s)
    except Exception as e:  # noqa: BLE001
        print(f"[luccy_qdrant] bundle check failed ({type(e).__name__}: {str(e)[:160]}); "
              "keeping the current bundle", file=sys.stderr)
        return _STATE["identity"] or (PurePosixPath(s.kw_alias), "")
    r = _STATE["retriever"]
    if r is not None and b.identity == r.bundle and b.refreshed_at:
        r.index_built = b.refreshed_at                 # a sync that found nothing new
    if _STATE["identity"] is None:
        _STATE["identity"] = b.identity
    return b.identity


def page_meta(page_id: str) -> dict | None:
    return current_live().page_meta(str(page_id))


def ticket_line(p: dict) -> str:
    """luccy builds this line from a Jira page's frontmatter (issuetype, status, priority,
    people, parent epic). DataGov's export carries none of those fields, and luccy returns ""
    for a page without an issue type, so every page has no ticket line."""
    return ""


def page_owners(doc_id: str, pages_dir=None) -> tuple:
    """luccy reads created_by / last_updated_by from the page file; DataGov keeps neither."""
    return ()


def pages_from_ids(self, question, ids, sources=None):
    r = self.retriever()
    live = getattr(r, "live", None)
    orig = _STATE["orig"]["serving.driver:AnswerService._pages_from_ids"]
    if live is None:
        return orig(self, question, ids, sources)
    with live.scope(ids):
        return orig(self, question, ids, sources)


def section_read(retriever, question: str, merged: list[dict]) -> tuple[str, dict]:
    orig = _STATE["orig"]["serving.driver:section_read"]
    live = getattr(retriever, "live", None)
    if live is None or not merged:
        return orig(retriever, question, merged)
    with live.scope([merged[0]["id"]]):
        return orig(retriever, question, merged)


def jira_tickets() -> list[dict]:
    return []


def catalog_row(doc_id: str) -> dict | None:
    return current_live().catalog_row(doc_id)


def bundle_problem(qc, index_dir) -> str | None:
    try:
        info, reasons = current_live().check()
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:300]}"
    return "; ".join(reasons) or None


def replacement_collection(vs) -> str | None:
    return None


def health():
    """/health with luccy's HealthResponse shape, from Qdrant instead of bundle files."""
    from fastapi.responses import JSONResponse

    from serving import config, jobs, spend
    from serving.api import app as luccy_app
    jobs._restart_if_bundle_moved()
    s = settings()
    reasons = []
    index = {"dir": f"qdrant:{s.kw_alias}", "sig": None, "age_hours": None, "chunk_count": None}
    qinfo = {"collection": s.chunks, "alias": None}
    corpus_pages = refreshed = None
    try:
        live = current_live()
        b = live.bundle
        index = {"dir": f"qdrant:{b.name}", "sig": b.sig, "age_hours": hours_since(b.refreshed_at),
                 "chunk_count": b.n}
        corpus_pages, refreshed = b.pages, b.refreshed_at or None
        qinfo, bad = live.check()
        reasons += bad
    except BundleMissing as e:          # before the first sync: say what to run
        reasons.append(str(e))
    except Exception as e:  # noqa: BLE001
        reasons.append(f"live corpus unavailable: {type(e).__name__}: {str(e)[:300]}")
    if not jobs.models_loaded():
        reasons.append("models not loaded" + (f": {jobs.warm_error}" if jobs.warm_error else ""))
    body = {"version": luccy_app.VERSION, "git": luccy_app.STAMP.get("git"), "index": index,
            "models_loaded": jobs.models_loaded(), "corpus_pages": corpus_pages,
            "refresh_last_ok": refreshed,
            "spend": {k: v for k, v in spend.cached_state(config.LOG_DIR).items()
                      if k != "unknown_example"},
            "qdrant": qinfo}
    if config.STORE == "postgres":
        from serving import store
        body["store"] = store.health()
    body = {"status": "ok" if not reasons else "unhealthy", **body}
    if reasons:
        body["reasons"] = reasons
    return JSONResponse(body, status_code=200 if not reasons else 503)


IMPLS = {
    "retrieve:Retriever.__init__": live_init,
    "retrieve:Retriever.retrieve": retrieve_wrapped,
    "retrieve:Retriever._source_keep": source_keep,
    "retrieve:bundle_identity": bundle_identity,
    "serving.rqb:page_meta": page_meta,
    "serving.rqb:ticket_line": ticket_line,
    "ingest.redact:page_owners": page_owners,
    "serving.driver:AnswerService._pages_from_ids": pages_from_ids,
    "serving.driver:section_read": section_read,
    "serving.deep:jira_tickets": jira_tickets,
    "serving.api.page_routes:_catalog_row": catalog_row,
    "ingest.qdrant_index:bundle_problem": bundle_problem,
    "ingest.qdrant_index:replacement_collection": replacement_collection,
}
# Names other luccy modules imported by value (`from x import y`), patched with the original.
ALSO_BOUND = {"ingest.redact:page_owners": ["serving.driver:page_owners"]}


def install() -> None:
    """Check the pins (a difference is a warning, verify(strict=False)), then swap every
    hooked attribute. Idempotent. Call it before luccy's web app is imported; the /health
    route is swapped by install_health(app) after."""
    with _lock:
        if _STATE["installed"]:
            return
        verify(routes=False, strict=False)
        for target, kind, _ in HOOKS:
            if kind == "route":
                continue
            _, owner, name, orig = resolve(target)
            _STATE["orig"][target] = orig
            setattr(owner, name, IMPLS[target])
            for alias in ALSO_BOUND.get(target, []):
                _, a_owner, a_name, a_obj = resolve(alias)
                if a_obj is not orig:
                    warn(f"{alias} is not {target}; luccy changed how it imports it (left as luccy has it)")
                    continue
                setattr(a_owner, a_name, IMPLS[target])
        _STATE["installed"] = True


def install_health(app) -> None:
    """Swap luccy's /health route for the live one, in place (same path, model and order)."""
    from fastapi.routing import APIRoute

    from serving.api import app as luccy_app
    from serving.api.schemas import HealthResponse

    from .pins import PINS
    if source_hash(luccy_app.health) != PINS["serving.api.app:health"]:
        warn("serving.api.app:health differs from the pinned version")
    idx = next((i for i, r in enumerate(app.router.routes)
                if isinstance(r, APIRoute) and r.path == "/health"), None)
    if idx is None or app.router.routes[idx].endpoint is not luccy_app.health:
        if idx is not None and app.router.routes[idx].endpoint is health:
            return
        warn("luccy's /health route is not where it was pinned; it is left as luccy has it")
        return
    app.router.routes.pop(idx)
    app.add_api_route("/health", health, methods=["GET"], response_model=HealthResponse,
                      responses={503: {"model": HealthResponse}})
    app.router.routes.insert(idx, app.router.routes.pop())


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if "--print" in argv:
        from serving import version
        print(f'LUCCY_VERSION = "{version.VERSION}"\nPINS = {{')
        for t, h in current_pins().items():
            print(f'    "{t}": "{h}",')
        print("}")
        return
    verify()
    if "--app" in argv:
        from . import app  # noqa: F401 - installs every hook and swaps /health, as the worker does
        if _STATE["warnings"]:
            raise SystemExit("luccy_qdrant: the hooks do not fit this luccy:\n  " + "\n  ".join(_STATE["warnings"]))
    print(f"luccy_qdrant: {len(pinned_targets())} pinned luccy functions match; {len(HOOKS)} hooks"
          + (", installed on luccy's app" if "--app" in argv else ""))


if __name__ == "__main__":
    main()
