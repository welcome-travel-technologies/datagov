"""Two Qdrant keys: the web process reads with Qdrant's READ-ONLY key, the sync writes with the
read-write key, and a sync handed the read-only key fails cleanly (nothing changed).

Needs a Qdrant started with QDRANT__SERVICE__API_KEY and QDRANT__SERVICE__READ_ONLY_API_KEY,
named by QDRANT_TEST_URL, QDRANT_TEST_API_KEY and QDRANT_TEST_READ_ONLY_API_KEY (conftest.py);
skipped otherwise. In that mode the rest of the suite runs the same way: the equality
battery and the e2e app (luccy's whole web process) serve with the read-only key.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys

import pytest

from .conftest import QDRANT, QDRANT_KEY, QDRANT_RO_KEY, RUN, live_env


@pytest.fixture
def keyed(qc):
    if not (QDRANT_KEY and QDRANT_RO_KEY):
        pytest.skip("QDRANT_TEST_API_KEY / QDRANT_TEST_READ_ONLY_API_KEY name no two-key Qdrant")


def read_only(s):
    """Settings whose every key, the sync's included, is the read-only one."""
    return dataclasses.replace(s, qdrant_api_key=QDRANT_RO_KEY, qdrant_sync_api_key="")


def state(qc, s) -> dict:
    """What a sync could have changed: alias, keyword collections, `luccy` blocks."""
    from luccy_qdrant.live import alias_target
    from luccy_qdrant.sync import has_luccy
    return {"alias": alias_target(qc, s.kw_alias),
            "collections": sorted(c.name for c in qc.get_collections().collections
                                  if c.name.startswith((s.kw_prefix, s.df_prefix))),
            "with_luccy": qc.count(s.chunks, count_filter=has_luccy(), exact=True).count}


def test_the_read_only_key_reads_and_cannot_write(qc, keyed):
    from qdrant_client import QdrantClient
    from qdrant_client.http.exceptions import UnexpectedResponse
    ro = QdrantClient(url=QDRANT, api_key=QDRANT_RO_KEY, timeout=30)
    ro.get_collections()
    with pytest.raises(UnexpectedResponse) as e:
        ro.create_collection(f"{RUN}_ro_probe", vectors_config={})
    assert e.value.status_code == 403


def test_sync_with_the_read_only_key_fails_cleanly(qc, keyed, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import read_bundle
    s, ro = world.s, read_only(world.s)
    assert ro.sync_key == QDRANT_RO_KEY                      # no sync key: falls back to the serving key
    before = state(qc, s)
    assert before == {"alias": None, "collections": [], "with_luccy": 0}
    # first run (no bundle yet): refused at its first write, before anything changed
    with pytest.raises(sync.SyncError, match="read-only.*LUCCY_QDRANT_SYNC_API_KEY") as e:
        sync.run(ro)
    assert "403" in str(e.value)
    assert state(qc, s) == before
    # as the scheduled job runs it: exit 1, one JSON line with the reason, no traceback
    env = {**os.environ, **live_env(ro), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("LUCCY_QDRANT_SYNC_API_KEY")
    done = subprocess.run([sys.executable, "-m", "luccy_qdrant.sync"], env=env, capture_output=True,
                          text=True, timeout=600)
    assert done.returncode == 1, done.stderr[-3000:]
    body = json.loads(done.stdout.strip().splitlines()[-1])
    assert body["status"] == "failed" and "LUCCY_QDRANT_SYNC_API_KEY" in body["error"]
    assert "Traceback" not in done.stderr and state(qc, s) == before
    # with a bundle in place (written with the read-write key), an unchanged run with the
    # read-only key is refused too and leaves the bundle as it was
    first = sync.run(s)
    built = state(qc, s)
    stamp = read_bundle(qc, s).refreshed_at
    with pytest.raises(sync.SyncError, match="read-only"):
        sync.run(ro)
    assert state(qc, s) == built and read_bundle(qc, s).refreshed_at == stamp
    assert read_bundle(qc, s).digest == first["digest"]


def test_sync_writes_with_the_sync_key_and_serving_reads_with_the_read_only_key(qc, keyed, world):
    from luccy_qdrant import sync
    from luccy_qdrant.live import Live, LiveKwStem
    s = world.s
    assert (s.sync_key, s.qdrant_api_key) == (QDRANT_KEY, QDRANT_RO_KEY)
    r = sync.run(s)                                          # sync: the read-write key
    assert r["status"] == "built" and r["points"] == world.n
    live = Live(s)                                           # serving: the read-only key
    info, reasons = live.check()
    assert reasons == [] and info["alias"] == r["bundle"]
    pdf = next(d for d in world.docs if d["key"] == "pdf3")
    pid = f"gd-dg-{pdf['document_id']}"
    assert live.page_body(pid) and live.page_meta(pid)["title"] == "Lost luggage policy"
    assert live.catalog_row(pid)["space"] == "Uploads"
    sc = LiveKwStem(live).scores("lost luggage policy")
    assert sc.max() > 0
    from .fakes import fake_vector
    rows = live.vector_search(fake_vector("lost luggage policy"), 10)
    assert rows and live.chunk(rows[0])["page_id"]
    assert sync.run(s)["status"] == "unchanged"              # the sync key again, read-only untouched
