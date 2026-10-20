"""DataGov document -> luccy page identity (contract C3), checked against luccy's own rules.

luccy decides a page's source from its id, space and link alone (`retrieve.source_of`): an
id starting `gd` is Google Drive, space `PBI` is Power BI, a github.com link is GitHub, a
`/wiki/` link or an all-digit id is Confluence, and anything else is Jira. Its citation
scanners (`serving/driver.py` PROSE_DOC_REF, `serving/claim_ground.py` _DOC_REF) read a cited
id as `[A-Za-z0-9][A-Za-z0-9_-]{3,}`, and its document viewer serves only ids its
DOC_ID_RE (`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`) matches. So:

  confluence:<id>            -> <id>                 (luccy's Confluence ids are the page id)
  confluence:<id>:part:<n>   -> <id>-part-<n>        ADJUSTED from C3's `<id>:part:<n>`: a ":"
                                                     stops both citation scanners, so the
                                                     number and grounding checks would read
                                                     the base page id instead
  jira:<KEY>                 -> <KEY>                (luccy's Jira ids are the issue key,
                                                     ingest/jira_ingest.py)
  upload                     -> gd-dg-<document_id>  ADJUSTED from C3's `dg-<document_id>`:
                                                     only a `gd` prefix makes source_of say
                                                     "drive"; a `dg-` id would read as Jira
  anything else              -> dg-<document_id>     when source_of then gives the intended
                                                     source (GitHub needs a github.com link,
                                                     Power BI gets space PBI); otherwise
                                                     gd-dg-<document_id> (drive), reported

An id another document already took, or one failing the patterns above, falls back the
same way.

This module is the SINGLE source of truth for luccy ids: the sync writes them into the `luccy`
payload block, and the comparison runner (luccy_qdrant.compare) imports the same functions to
map DataGov documents into luccy's id space. Stable public API (signatures and results do
not change without a migration of every synced bundle):

    luccy_id_for(row) -> str          one C2 export row (external_id, document_id,
                                      source_type, source_url, collection_name / group)
    luccy_ref_for(row) -> dict        {id, source, space, url, kind, collision}
    assign(rows) -> (refs, report)    a whole export: {document_id: ref} plus counts,
                                      resolving two rows that claim one natural id

`assign()` is what the sync runs. For every row whose natural id no other row claims,
`assign(rows)[0][row["document_id"]]["id"] == luccy_id_for(row)`, whatever the other rows and
their order. The id never depends on `upload_url_template` (only the stored link does).
Every id is checked with luccy's own `source_of`, so the source stored in the payload is
exactly what luccy computes for that page everywhere it asks. Callers need luccy
(`retrieve`) and the engine (`document_explorer.processing`) importable, as in the image.
"""
from __future__ import annotations

import re

C3_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}$")
VIEWER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")      # page_routes.DOC_ID_RE
CITE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}")              # PROSE_DOC_REF / _DOC_REF
CONFLUENCE_RE = re.compile(r"^(?i:confluence):(\d+)$")
CONFLUENCE_PART_RE = re.compile(r"^(?i:confluence):(\d+):(?i:part):(\d+)$")
JIRA_RE = re.compile(r"^(?i:jira):([A-Z][A-Z0-9_]*-\d+)$")
# DataGov source_type -> the luccy source it should read as.
WANT = {"confluence": "confluence", "jira": "jira", "upload": "drive", "github": "github",
        "dbt": "github", "powerbi": "powerbi"}
SOURCES = ("confluence", "jira", "drive", "github", "powerbi")


def valid(luccy_id: str) -> bool:
    return bool(C3_ID_RE.match(luccy_id) and ".." not in luccy_id
                and VIEWER_ID_RE.fullmatch(luccy_id) and CITE_ID_RE.fullmatch(luccy_id))


def natural_id(external_id: str) -> str | None:
    """The id luccy itself would give the page, or None for a source luccy names by hash."""
    ext = external_id or ""
    if m := CONFLUENCE_RE.match(ext):
        return m.group(1)
    if m := CONFLUENCE_PART_RE.match(ext):
        return f"{m.group(1)}-part-{m.group(2)}"
    if m := JIRA_RE.match(ext):
        return m.group(1)
    return None


def space_of(doc: dict, natural: str | None) -> str:
    """luccy's `space` for the page: PBI for Power BI (source_of needs it), the project key
    of a Jira issue, else the DataGov collection name (DataGov keeps no Confluence space key)."""
    if (doc.get("source_type") or "") == "powerbi":
        return "PBI"
    if natural and (doc.get("source_type") or "") == "jira":
        return natural.rsplit("-", 1)[0]
    name = " ".join(str(doc.get("collection_name") or doc.get("group") or "").split())
    return name if name and name != "PBI" else "?"


def link_of(doc: dict, upload_url_template: str = "") -> str:
    """The page link: the C2 row's source URL through the engine's own `safe_source_url` (so
    it is exactly what DataGov's chunk points carry), else for an upload the optional DataGov
    reader template, else ""."""
    from document_explorer.processing import safe_source_url
    url = safe_source_url(doc.get("source_url") or "")
    if url:
        return url
    if (doc.get("source_type") or "") == "upload" and upload_url_template:
        return upload_url_template.format(document_id=doc["document_id"],
                                          version_id=doc.get("version_id", ""))
    return ""


def luccy_ref_for(document: dict, upload_url_template: str = "", taken: set | None = None) -> dict:
    """{"id", "source", "space", "url", "kind"} for ONE C2 export row (external_id,
    document_id, source_type, source_url, collection_name / group). `taken`: ids already
    given to other documents (assign() passes it; a natural id taken falls back)."""
    import retrieve                      # luccy's own source_of (a cheap module import)

    taken = taken if taken is not None else set()
    want = WANT.get(document.get("source_type") or "", "drive")
    natural = natural_id(document.get("external_id") or "")
    url = link_of(document, upload_url_template)
    space = space_of(document, natural)
    tries = []
    if natural:
        tries.append(("natural", natural))
    if want != "drive":
        tries.append(("dg", f"dg-{document['document_id']}"))
    tries.append(("gd_dg", f"gd-dg-{document['document_id']}"))
    for kind, cand in tries:
        if not valid(cand) or cand in taken:
            continue
        got = retrieve.source_of(cand, space, url)
        if kind != "gd_dg" and got != want:
            continue
        return {"id": cand, "source": got, "space": space, "url": url, "kind": kind,
                "collision": bool(natural) and natural in taken}
    raise RuntimeError(f"no luccy id for document {document['document_id']}")  # pragma: no cover


def luccy_id_for(document: dict) -> str:
    """The luccy page id of one C2 export row, as the sync gives it (STABLE PUBLIC API; the
    compare runner imports it). Two rows can claim one natural id only if DataGov holds the
    same Confluence page or Jira issue twice; `assign()` resolves that for a whole export
    (the first by (external_id, document_id) keeps it), this function cannot see the others."""
    return luccy_ref_for(document)["id"]


def assign(documents: list[dict], upload_url_template: str = "") -> tuple[dict[str, dict], dict]:
    """{document_id: {"id", "source", "space", "url", "kind", "collision"}} for a whole export,
    and a report (STABLE PUBLIC API; the sync and the compare runner both call it).

    Deterministic and independent of the input order: documents are visited by
    (external_id, document_id), so of two documents claiming one natural id the first keeps
    it and the other falls back."""
    taken: set[str] = set()
    out: dict[str, dict] = {}
    report = {"natural": 0, "dg": 0, "gd_dg": 0, "collisions": 0, "reclassified_drive": 0,
              "by_source": {s: 0 for s in SOURCES}}
    for doc in sorted(documents, key=lambda d: (d.get("external_id") or "", d["document_id"])):
        ref = luccy_ref_for(doc, upload_url_template, taken)
        taken.add(ref["id"])
        report[ref["kind"]] += 1
        report["collisions"] += ref["collision"]
        if ref["source"] != WANT.get(doc.get("source_type") or "", "drive"):
            report["reclassified_drive"] += 1
        report["by_source"][ref["source"]] += 1
        out[doc["document_id"]] = ref
    return out, report
