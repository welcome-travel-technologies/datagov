"""The one id space every arm is compared in: luccy page ids.

A DataGov document's luccy id comes from the analyzer's own rule, the single source of
truth in luccy_qdrant/ids.py (`assign` over the whole listing, so two documents claiming
one id resolve exactly as the sync resolves them):

    confluence:<id>            -> <id>
    confluence:<id>:part:<n>   -> <id>-part-<n>      (one part of a long page)
    jira:<KEY>                 -> <KEY>
    an upload, anything else   -> gd-dg-<document_id> (or dg-<document_id>, see ids.py)

The gold set names luccy's original page ids (a Confluence page id, a Jira key). A part id
counts as its page wherever a gold id is matched (page_of accepts both `<id>-part-<n>` and
C3's `<id>:part:<n>`), so a split page is still the page the gold row expects. A citation
that carries a `luccy_id` keeps it: it is the id the analyzer assigned. A gold row whose
documents DataGov does not hold is still asked and scored; `Corpus.missing` names them.
"""
from __future__ import annotations

import re

from .. import ids as adapter_ids

PART_RE = re.compile(r"^(.+?)(?::part:|-part-)\d+$")
REGISTRY_PREFIX = "registry:"
# A cited document outside the listed corpus gets the form ids.py gives a document it cannot
# place otherwise; it never matches a gold page.
UNLISTED_PREFIX = "gd-dg-"


def unique(values) -> list:
    """Order-preserving de-duplication that drops empty values."""
    return list(dict.fromkeys(v for v in values if v))


def page_of(lid: str) -> str:
    """The page a luccy id belongs to: '123-part-2' and '123:part:2' -> '123'; any other id
    is its own page."""
    m = PART_RE.match(str(lid))
    return m.group(1) if m else str(lid)


def expected_groups(row: dict) -> list[str]:
    """The gold row's required document groups ("a|b" = either), warehouse registry keys left out."""
    return [g for g in row.get("expected_doc_ids") or [] if g and not g.startswith(REGISTRY_PREFIX)]


def groups_hit(groups: list[str], cited) -> bool:
    """Every required group is met by at least one cited page (part ids count as their page)."""
    pages = {page_of(c) for c in cited or []}
    return all({page_of(x) for x in g.split("|")} & pages for g in groups)


class Corpus:
    """DataGov's in-scope documents, keyed for the comparison by the analyzer's own ids."""

    def __init__(self, documents: list[dict]):
        self.by_document: dict[str, str] = {}          # DataGov document_id -> luccy id
        self.document_of_page: dict[str, str] = {}     # luccy page id -> one document_id (fakes)
        self.ids: set[str] = set()
        self.pages: set[str] = set()
        self.skipped = 0
        rows = []
        for doc in documents:
            document_id = str((doc.get("document_id") or doc.get("id") or "") if isinstance(doc, dict) else "").strip()
            if not document_id:
                self.skipped += 1
                continue
            rows.append(dict(doc, document_id=document_id))
        refs, self.report = adapter_ids.assign(rows) if rows else ({}, {})
        for row in sorted(rows, key=lambda d: d["document_id"]):
            lid = refs[row["document_id"]]["id"]
            self.by_document[row["document_id"]] = lid
            self.ids.add(lid)
            self.pages.add(page_of(lid))
            self.document_of_page.setdefault(page_of(lid), row["document_id"])

    def __len__(self) -> int:
        return len(self.by_document)

    def cited_id(self, citation) -> str | None:
        """A DataGov citation in luccy ids: the luccy_id it carries, else its document_id through
        the analyzer's mapping of the listed corpus, else gd-dg-<document_id> (a document outside
        the listed corpus)."""
        if isinstance(citation, str):
            return citation.strip() or None
        if not isinstance(citation, dict):
            return None
        lid = str(citation.get("luccy_id") or "").strip()
        if lid:
            return lid
        document_id = str(citation.get("document_id") or "").strip()
        if document_id and document_id in self.by_document:
            return self.by_document[document_id]
        return UNLISTED_PREFIX + document_id if document_id else None

    def has_group(self, group: str) -> bool:
        return any(page_of(x) in self.pages for x in group.split("|"))

    def missing(self, row: dict) -> list[str]:
        """The gold row's required document groups DataGov's corpus does not hold ("a|b": neither).
        The row is asked and scored anyway; this only explains a failure."""
        return [g for g in expected_groups(row) if not self.has_group(g)]
