"""The sync job: payload on exported versions only, the keyword collection behind the alias,
the digest shortcut, export changes, rollback copy, refusals, versions left out instead of a
failed run. Each test builds its own world (conftest `world`: own chunk collection, alias and
fake export) on the throwaway Qdrant."""
from __future__ import annotations

import copy
import dataclasses
import json
import uuid

import pytest
from qdrant_client import models

from .conftest import engine_upsert
from .corpus import ORG


def payloads(qc, s):
    out, offset = {}, None
    while True:
        pts, offset = qc.scroll(s.chunks, limit=1024, offset=offset, with_payload=["version_id", "luccy"],
                                with_vectors=False)
        for p in pts:
            out[str(p.id)] = p.payload
        if offset is None:
            return out


def kw_collections(qc, s):
    return sorted(c.name for c in qc.get_collections().collections if c.name.startswith(s.kw_prefix))


def test_first_sync_builds_everything(qc, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import alias_target, read_bundle
    s = world.s
    r = sync.run(s)
    assert r["status"] == "built" and r["chunks"] == world.n
    assert r["documents_in_scope"] == r["documents_synced"] == len(world.docs)
    assert r["documents_without_chunks"] == 0 and r["payload_removed_versions"] == 0
    assert r["ids"]["by_source"] == {"confluence": 6, "jira": 2, "drive": 3, "github": 1, "powerbi": 1}
    b = read_bundle(qc, s)
    assert alias_target(qc, s.kw_alias) == b.name == r["bundle"]
    assert (b.n, b.pages, b.organization_id, b.chunks) == (world.n, len(world.docs), ORG, s.chunks)
    assert b.digest == r["digest"] and qc.count(b.name, exact=True).count == world.n
    assert qc.count(b.df_name, exact=True).count == r["vocabulary"] > 100
    pl = payloads(qc, s)
    assert all("luccy" in p for p in pl.values())
    kw = {str(p.id): p.payload for p in qc.scroll(b.name, limit=10000, with_payload=True)[0]}
    assert kw.keys() == pl.keys()                                  # same point ids
    assert sorted(p["row"] for p in kw.values()) == list(range(world.n))
    assert all(kw[i]["luccy"] == pl[i]["luccy"] for i in kw)
    # rows follow luccy's page-file order, then page, then ordinal
    full = {str(p.id): p.payload for p in qc.scroll(s.chunks, limit=10000, with_payload=["page", "ordinal"])[0]}
    order = sorted(kw, key=lambda i: kw[i]["row"])
    keys = [(kw[i]["luccy"]["id"] + ".md", full[i]["page"], full[i]["ordinal"]) for i in order]
    assert keys == sorted(keys)
    assert b.missing_versions == () and r["skipped_versions"] == {} and r["points"] == world.n
    # nothing is reported back to DataGov: the sync only reads its export
    assert not [path for path in world.c2.requests if path.startswith("POST")]


def test_unchanged_digest_skips(qc, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import read_bundle
    s = world.s
    first = sync.run(s)
    before = read_bundle(qc, s)
    again = sync.run(s)
    assert again["status"] == "unchanged" and again["bundle"] == first["bundle"]
    after = read_bundle(qc, s)
    assert after.refreshed_at > before.refreshed_at and after.built_at == before.built_at
    assert kw_collections(qc, s) == [first["bundle"]]
    assert again["points"] == world.n and again["digest"] == first["digest"]
    # a dry run reads only: no write
    dry = sync.run(s, dry_run=True)
    assert dry["status"] == "unchanged"
    assert read_bundle(qc, s).refreshed_at == after.refreshed_at
    forced = sync.run(s, force=True)
    assert forced["status"] == "built" and forced["sig"] == first["sig"] and forced["previous"] == first["bundle"]


def test_main_prints_the_report_and_exits_zero(qc, world, monkeypatch, capsys):
    from luccy_qdrant import sync
    from luccy_qdrant.settings import Settings
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: world.s))
    assert sync.main([]) == 0                                                 # as the job runs it
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "built" and body["points"] == world.n


def test_export_changes_move_payload_alias_and_prune(qc, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import read_bundle
    s, c2 = world.s, world.c2
    first = sync.run(s)
    # a document leaves the scope, another gets a new version (re-indexed by the engine)
    gone = next(d for d in c2.documents if d["key"] == "conf2")
    changed = next(d for d in c2.documents if d["key"] == "conf4")
    new = copy.deepcopy(changed)
    new["pages"] = [changed["pages"][0] + "\n\n## Addendum\n\n" + "New paragraph about the refund policy. " * 12]
    new["version_id"] = str(uuid.uuid4())
    new["content_hash"] = uuid.uuid4().hex * 2
    engine_upsert(qc, s.chunks, [new], world.tmp, ORG)
    c2.documents = [d for d in c2.documents if d["key"] not in ("conf2", "conf4")] + [new]
    second = sync.run(s)
    assert second["status"] == "built" and second["previous"] == first["bundle"]
    assert second["payload_removed_versions"] == 2                   # conf2 and conf4's old version
    pl = payloads(qc, s)
    for p in pl.values():
        has = "luccy" in p
        assert has == (p["version_id"] not in (gone["version_id"], changed["version_id"]))
    assert read_bundle(qc, s).name == second["bundle"]
    assert kw_collections(qc, s) == sorted([first["bundle"], second["bundle"]])   # previous kept
    c2.documents = [d for d in c2.documents if d["key"] != "jira0"]
    third = sync.run(s)
    assert kw_collections(qc, s) == sorted([second["bundle"], third["bundle"]])   # oldest pruned
    names = {c.name for c in qc.get_collections().collections}
    assert s.df_name(first["bundle"]) not in names and s.df_name(third["bundle"]) in names
    # the filtered count (payload index) agrees with the payloads after removals, so the
    # next run is a cheap "unchanged" and reports the true number of points
    with_block = sum("luccy" in p for p in payloads(qc, s).values())
    assert second["points"] == second["chunks"] and third["points"] == third["chunks"] == with_block
    fourth = sync.run(s)
    assert fourth["status"] == "unchanged" and fourth["points"] == third["chunks"]
    # a block removed the old way (`luccy` only: Qdrant keeps the nested index entries) still
    # matches the filters; the next sync finds it through them and heals it
    flt = models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=gone["version_id"]))])
    qc.set_payload(s.chunks, payload={"luccy": {"id": "x", "source": "confluence"}}, points=flt, wait=True)
    qc.delete_payload(s.chunks, keys=["luccy"], points=flt, wait=True)
    assert qc.count(s.chunks, count_filter=sync.has_luccy(ORG), exact=True).count > third["chunks"]
    healed = sync.run(s)
    assert healed["status"] == "repaired" and healed["payload_removed_versions"] == 1
    assert healed["points"] == third["chunks"] == qc.count(s.chunks, count_filter=sync.has_luccy(ORG), exact=True).count
    assert sync.run(s)["status"] == "unchanged"


def test_live_reads_on_the_synced_bundle(qc, world):
    """A multi-page upload is rebuilt over (page, ordinal); bundle identity follows the alias."""
    from document_explorer.stitch import normalize

    from luccy_qdrant import sync
    from luccy_qdrant.live import Live, read_bundle
    s = world.s
    sync.run(s)
    live = Live(s)
    pdf = next(d for d in world.docs if d["key"] == "pdf3")
    rec = live.page(f"gd-dg-{pdf['document_id']}")
    body = live.page_body(f"gd-dg-{pdf['document_id']}")
    assert normalize(body) == normalize("\n\n".join(pdf["pages"]))
    assert len(rec["rows"]) > 3
    meta = live.page_meta(f"gd-dg-{pdf['document_id']}")
    assert meta["title"] == "Lost luggage policy" and meta["space"] == "Uploads" and meta["url"] == ""
    assert live.page_meta("no-such-page") is None and live.catalog_row("no-such-page") is None
    identity = live.bundle.identity
    world.c2.documents = world.c2.documents[1:]
    sync.run(s)
    assert read_bundle(qc, s).identity != identity
    assert Live(s).bundle.identity != identity


def test_datagov_calls_ignore_proxy_settings(monkeypatch):
    """DataGov answers 404 to an internal call that came through a proxy (X-Forwarded-For,
    X-Real-IP, Forwarded), so the export and the digest go straight to DATAGOV_URL whatever
    HTTP(S)_PROXY says."""
    from luccy_qdrant import sync
    from luccy_qdrant.settings import Settings

    from .conftest import KEYS, TOKEN
    from .corpus import ORG, corpus
    from .fakes import C2Server
    c2 = C2Server(ORG, [d for d in corpus() if d["key"] in KEYS])
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")            # nothing listens there
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        s = dataclasses.replace(Settings.from_env(), datagov_url=c2.url, sync_token=TOKEN)
        digest = sync.c2_digest(s)
        org, docs = sync.c2_documents(s)
        assert (digest["organization_id"], org) == (ORG, ORG)
        assert digest["digest"] == sync.version_digest(docs)
    finally:
        c2.close()


def test_refusals_leave_the_alias(qc, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import alias_target
    s = world.s
    first = sync.run(s)
    bad = dataclasses.replace(s, sync_token="wrong")
    with pytest.raises(sync.SyncError, match="HTTP 401"):
        sync.run(bad)
    world.c2.documents = []
    with pytest.raises(sync.SyncError, match="empty corpus"):
        sync.run(s)
    assert alias_target(qc, s.kw_alias) == first["bundle"]


def test_missing_points_and_ordinals(qc, world, monkeypatch):
    from luccy_qdrant import sync
    from luccy_qdrant.live import Live, read_bundle
    s, c2 = world.s, world.c2
    ghost = copy.deepcopy(c2.documents[0])
    ghost.update(document_id=str(uuid.uuid4()), version_id=str(uuid.uuid4()), external_id="confluence:1234567")
    c2.documents.append(ghost)                                      # exported, never indexed
    r = sync.run(s)
    assert r["documents_without_chunks"] == 1 and ghost["version_id"] in r["missing_versions"]
    b = read_bundle(qc, s)
    assert b.digest == r["digest"] == sync.version_digest(c2.documents)
    assert b.missing_versions == (ghost["version_id"],)
    # still without points: one filtered count, no rebuild (and no reload of the analyzer)
    again = sync.run(s)
    assert again["status"] == "unchanged" and again["bundle"] == r["bundle"]
    # once the engine indexes it, the next run rebuilds with it
    engine_upsert(qc, s.chunks, [ghost], world.tmp, ORG)
    rebuilt = sync.run(s)
    assert rebuilt["status"] == "built" and rebuilt["reason"] == "versions that had no chunk points have some now"
    assert rebuilt["documents_without_chunks"] == 0 and read_bundle(qc, s).missing_versions == ()
    c2.documents.remove(ghost)
    dry = sync.run(s, force=True, dry_run=True)
    assert dry["status"] == "dry-run" and dry["chunks"] == world.n
    # a point written before ordinals (not yet backfilled): served, never fed whole
    doc = next(d for d in world.docs if d["key"] == "conf0")
    flt = models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=doc["version_id"]))])
    pid = qc.scroll(s.chunks, scroll_filter=flt, limit=1)[0][0].id
    qc.delete_payload(s.chunks, keys=["ordinal"], points=[pid], wait=True)
    r = sync.run(s, force=True)
    assert r["status"] == "built" and r["versions_without_ordinal"] == 1 and r["chunks"] == world.n
    live = Live(s)
    page = "6800000000"
    assert live.page_body(page) == "" and live.page_meta(page) is None   # luccy's "cannot read"
    assert live.catalog_row(page)["title"] == doc["title"]                # still in the corpus
    from serving import rqb

    from luccy_qdrant import hooks
    monkeypatch.setitem(hooks._STATE, "live", live)
    monkeypatch.setattr(rqb, "page_meta", hooks.page_meta)            # H5, as installed
    merged = [{"id": page, "passages": [{"heading": "", "score": 1.0, "text": "x" * 10}]}]
    assert rqb.fill_whole_pages(merged) == [] and merged[0]["whole_skip"] == "empty"


def test_engine_reupsert_wipes_payload_and_sync_repairs(qc, world):
    """DataGov's engine re-upserts a version's points whole (payload included) when one of
    them is missing, which wipes the `luccy` block; the export digest does not change."""
    from luccy_qdrant import sync
    from luccy_qdrant.live import read_bundle
    s = world.s
    first = sync.run(s)
    doc = next(d for d in world.docs if d["key"] == "big0")
    engine_upsert(qc, s.chunks, [doc], world.tmp, ORG)                # same ids, payload replaced
    flt = models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=doc["version_id"]))])
    wiped = qc.count(s.chunks, count_filter=flt, exact=True).count
    assert wiped > 10 and all("luccy" not in (p.payload or {}) for p in qc.scroll(s.chunks, scroll_filter=flt, limit=1000)[0])
    r = sync.run(s)
    assert r["status"] == "repaired" and r["with_luccy_before"] == world.n - wiped
    assert r["bundle"] == first["bundle"] and kw_collections(qc, s) == [first["bundle"]]
    assert all(p.get("luccy") for p in payloads(qc, s).values())
    assert sync.run(s)["status"] == "unchanged"
    # the point set itself changed (a point deleted): a repair cannot fix that, a rebuild does
    pid = qc.scroll(s.chunks, scroll_filter=flt, limit=1)[0][0].id
    qc.delete(s.chunks, points_selector=models.PointIdsList(points=[pid]), wait=True)
    r = sync.run(s)
    assert r["status"] == "built" and r["reason"] == "chunk points changed under an unchanged export"
    assert r["chunks"] == world.n - 1 and read_bundle(qc, s).n == world.n - 1


def test_bad_versions_are_left_out_not_fatal(qc, world, monkeypatch):
    """A version the export lists twice, one whose points share a (page id, page, ordinal),
    and one whose points vanish mid-build are left out; the rest is published."""
    from luccy_qdrant import sync
    from luccy_qdrant.live import read_bundle
    s, c2 = world.s, world.c2
    twice = next(d for d in c2.documents if d["key"] == "conf0")
    c2.documents.append(copy.deepcopy(twice))
    shared = next(d for d in world.docs if d["key"] == "jira0")
    flt = models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=shared["version_id"]))])
    point = qc.scroll(s.chunks, scroll_filter=flt, limit=1, with_payload=True, with_vectors=True)[0][0]
    qc.upsert(s.chunks, points=[models.PointStruct(id=str(uuid.uuid4()), vector=point.vector, payload=point.payload)],
              wait=True)                                              # a second point at the same place
    gone = next(d for d in world.docs if d["key"] == "big0")
    flt_gone = models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=gone["version_id"]))])
    victim = qc.scroll(s.chunks, scroll_filter=flt_gone, limit=1)[0][0].id
    real, calls = sync.build_keyword, []

    def flaky(qc_, *args, **kwargs):
        if not calls:                                                 # the engine deletes a point meanwhile
            calls.append(1)
            qc_.delete(s.chunks, points_selector=models.PointIdsList(points=[victim]), wait=True)
        return real(qc_, *args, **kwargs)
    monkeypatch.setattr(sync, "build_keyword", flaky)
    r = sync.run(s)
    assert r["status"] == "built" and len(calls) == 1
    assert r["skipped_versions"] == {
        twice["version_id"]: "listed twice by the export",
        shared["version_id"]: "two chunk points share one (page id, page, ordinal)",
        gone["version_id"]: "chunk points vanished during the sync"}
    pl = payloads(qc, s)
    left_out = {twice["version_id"], shared["version_id"], gone["version_id"]}
    assert all(("luccy" in p) == (p["version_id"] not in left_out) for p in pl.values())
    assert read_bundle(qc, s).missing_versions == (gone["version_id"],)
    # the vanished version still has points: the next run picks it up again
    monkeypatch.setattr(sync, "build_keyword", real)
    again = sync.run(s)
    assert again["status"] == "built" and gone["version_id"] not in again["skipped_versions"]
    assert set(again["skipped_versions"]) == {twice["version_id"], shared["version_id"]}
