"""A synthetic, deterministic corpus in DataGov's shape: one document per entry, with its C2
export row and its page text(s). It exercises what luccy's chunker and evidence stages treat
specially: heading paths, long tables (row groups with a repeated header), long lists (items
never split), tiny sections (packed into their neighbour), pages over 20,000 characters
(passages, never whole), enough 18,000-character pages to fill the 240,000 total, numeric
Confluence ids, a Confluence part, Jira keys, a template, a period pair, GitHub, dbt, Power BI
and uploads. Text is clean (luccy's redaction leaves it unchanged, which corpus() asserts).
"""
from __future__ import annotations

import hashlib
import random
import uuid

ORG = "4242"
NS = uuid.UUID("2f6b5d1e-9c1a-4f7e-8b3c-6a5d4e3f2a10")
WIKI = "https://welcome.atlassian.net/wiki/spaces/{space}/pages/{id}"
BROWSE = "https://welcome.atlassian.net/browse/{key}"
GH = "https://github.com/welcome/dbt-analytics"

WORDS = ("transfer booking driver pickup airport revenue margin cancellation refund commission "
         "partner region quarter target forecast conversion upsell attribution channel report "
         "policy procedure checklist escalation invoice payout supplier fleet rating complaint "
         "delay greeting luggage voucher pricing discount surcharge night weekend season demand "
         "capacity dispatch allocation onboarding training quality audit dashboard metric "
         "retention cohort funnel campaign newsletter loyalty corporate account contract renewal "
         "tender operations support backlog release deployment warehouse model measure dataset "
         "pipeline schema column").split()
CITIES = ("Athens Lisbon Rome Barcelona Paris Milan Porto Seville Naples Crete Mykonos Santorini "
          "Madrid Vienna Prague Dublin").split()
QUARTERS = ("Q1", "Q2", "Q3", "Q4")


def _sentence(rng: random.Random, topic: list[str]) -> str:
    w = [rng.choice(topic if rng.random() < 0.6 else WORDS) for _ in range(6)]
    city = rng.choice(CITIES)
    n = rng.randint(3, 950)
    forms = [f"The {w[0]} {w[1]} for {city} changed by {n} percent after the {w[2]} review.",
             f"Every {w[0]} {w[1]} needs a {w[2]} check before the {w[3]} {w[4]} starts.",
             f"In {rng.choice(QUARTERS)} the {w[0]} team moved {n} {w[1]} cases to {w[2]} {w[3]}.",
             f"Our {w[0]} {w[1]} rule says the {w[2]} {w[3]} is owned by the {w[4]} desk in {city}.",
             f"When a {w[0]} {w[1]} fails, the {w[2]} {w[3]} is escalated within {n % 48 + 1} hours."]
    return rng.choice(forms)


def _paragraph(rng, topic, k=None) -> str:
    return " ".join(_sentence(rng, topic) for _ in range(k or rng.randint(3, 6)))


def _prose(rng, topic, paragraphs) -> str:
    return "\n\n".join(_paragraph(rng, topic) for _ in range(paragraphs))


def _table(rng, rows, topic) -> str:
    head = "| City | Metric | Q1 | Q2 | Q3 | Q4 |\n|---|---|---|---|---|---|"
    lines = [f"| {rng.choice(CITIES)} | {rng.choice(topic)} {rng.choice(WORDS)} | "
             + " | ".join(str(rng.randint(10, 9999)) for _ in range(4)) + " |" for _ in range(rows)]
    return head + "\n" + "\n".join(lines)


def _list(rng, items, topic, ordered=False) -> str:
    out = []
    for i in range(items):
        mark = f"{i + 1}." if ordered else "-"
        out.append(f"{mark} {_sentence(rng, topic)}")
    return "\n".join(out)


def _body(rng, title, topic, sections, *, table_rows=0, list_items=0, tiny=0, long_paras=0) -> str:
    parts = [_paragraph(rng, topic)]
    for s in range(sections):
        parts.append(f"## {rng.choice(topic).capitalize()} {rng.choice(WORDS)} {s + 1}")
        parts.append(_prose(rng, topic, rng.randint(1, 3)))
        if s == 0 and table_rows:
            parts.append(_table(rng, table_rows, topic))
        if s == 1 and list_items:
            parts.append(_list(rng, list_items, topic, ordered=bool(s % 2)))
        if s == 1:
            parts.append(f"### Details {s + 1}")
            parts.append(_prose(rng, topic, 1))
        if tiny and s < tiny:
            parts.append(f"## Note {s + 1}")
            parts.append(f"Short note: {rng.choice(topic)} {rng.choice(WORDS)} for {rng.choice(CITIES)}.")
    for i in range(long_paras):
        parts.append(f"## Chapter {i + 1}")
        parts.append(_prose(rng, topic, 4))
    return "\n\n".join(parts)


def _grow(rng, body, topic, target) -> str:
    i = 0
    while len(body) < target:
        i += 1
        body += f"\n\n## Annex {i}\n\n" + _prose(rng, topic, 3)
    return body


def _doc(key, external_id, title, source_type, url, collection, group, modified, pages) -> dict:
    content_hash = hashlib.sha256("\x00".join(pages).encode()).hexdigest()
    return {"key": key, "document_id": str(uuid.uuid5(NS, "doc:" + key)),
            "version_id": str(uuid.uuid5(NS, "ver:" + key + content_hash)),
            "content_hash": content_hash, "external_id": external_id, "title": title,
            "source_type": source_type, "source_url": url, "collection_id": str(uuid.uuid5(NS, collection)),
            "collection_name": collection, "group": group, "modified": modified, "pages": pages}


def corpus(seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    docs = []
    topics = [WORDS[i:i + 8] for i in range(0, len(WORDS) - 8, 6)]

    def mod(i):
        return f"2026-{(i % 9) + 1:02d}-{(i * 7) % 27 + 1:02d}T{(i * 5) % 24:02d}:15:00+00:00"

    # Confluence pages with mixed structure.
    for i in range(16):
        pid = 6800000000 + i * 7919
        topic = topics[i % len(topics)]
        title = f"{topic[0].capitalize()} {topic[1]} guide {CITIES[i % len(CITIES)]}"
        body = _body(rng, title, topic, rng.randint(2, 5), table_rows=60 if i in (2, 9) else (5 if i % 3 == 0 else 0),
                     list_items=45 if i in (4, 11) else (4 if i % 4 == 1 else 0), tiny=2 if i % 5 == 0 else 0)
        docs.append(_doc(f"conf{i}", f"confluence:{pid}", title, "confluence",
                         WIKI.format(space="COM", id=pid), "Confluence COM", "Welcome Confluence", mod(i), [body]))
    # Pages over 20,000 characters (never fed whole).
    for i, target in enumerate((24000, 31000, 47000)):
        pid = 6900000000 + i * 104729
        topic = topics[(i + 3) % len(topics)]
        body = _grow(rng, _body(rng, "", topic, 4, table_rows=30, list_items=12), topic, target)
        docs.append(_doc(f"big{i}", f"confluence:{pid}", f"Complete {topic[0]} manual volume {i + 1}",
                         "confluence", WIKI.format(space="OPS", id=pid), "Confluence OPS", "Welcome Confluence",
                         mod(30 + i), [body]))
    # Fourteen ~18,000-character handbooks: together they pass the 240,000 total.
    hand = ["dispatch", "allocation", "fleet", "capacity", "driver", "operations", "airport", "pickup"]
    for i in range(14):
        pid = 7100000000 + i * 15485
        body = _grow(rng, _body(rng, "", hand, 3, list_items=8), hand, 17500 + (i * 37) % 900)
        docs.append(_doc(f"hand{i}", f"confluence:{pid}", f"Regional operations handbook {CITIES[i]}",
                         "confluence", WIKI.format(space="OPS", id=pid), "Confluence OPS", "Welcome Confluence",
                         mod(40 + i), [body]))
    # A template, a period pair and a Confluence part.
    t = ["report", "metric", "target", "revenue", "dashboard", "quarter"]
    docs.append(_doc("tmpl", "confluence:6999000001", "Monthly KPI report template", "confluence",
                     WIKI.format(space="COM", id=6999000001), "Confluence COM", "Welcome Confluence", mod(60),
                     [_body(rng, "", t, 3, table_rows=6)]))
    q = ["revenue", "target", "partner", "region", "forecast", "quarter"]
    for k, (name, when) in enumerate((("Quarterly Business Review Q1 2024", "2024-04-02T09:00:00+00:00"),
                                      ("Quarterly Business Review Q2 2025", "2025-07-03T09:00:00+00:00"))):
        pid = 6999000100 + k
        docs.append(_doc(f"qbr{k}", f"confluence:{pid}", name, "confluence", WIKI.format(space="COM", id=pid),
                         "Confluence COM", "Welcome Confluence", when, [_body(rng, "", q, 3, table_rows=8)]))
    docs.append(_doc("part2", "confluence:6999000200:part:2", "Driver onboarding handbook (part 2)", "confluence",
                     WIKI.format(space="COM", id=6999000200), "Confluence COM", "Welcome Confluence", mod(70),
                     [_body(rng, "", ["driver", "onboarding", "training", "quality"], 3, list_items=10)]))
    # Jira issues in DataGov's form: key + summary title, status lines, a description.
    for i, (key, summary) in enumerate((("AP-520", "Refund not issued after cancellation"),
                                        ("AP-1043", "Driver payout delayed for weekend transfers"),
                                        ("ANI-77", "Attribution of upsell revenue to CRM channel"),
                                        ("PHOENIX-1724", "Dispatch allocation fails at airport pickup"),
                                        ("AP-9", "Voucher discount applied twice"))):
        topic = topics[(i + 5) % len(topics)]
        body = (f"Status: {('Done', 'In Progress', 'To Do')[i % 3]}\nType: {('Bug', 'Task')[i % 2]}\n"
                f"Updated: 2026-0{i + 1}-12\n\n## Description\n\n{_prose(rng, topic + summary.lower().split(), 2)}"
                f"\n\n## Discussion\n\n{_prose(rng, topic, 2)}")
        docs.append(_doc(f"jira{i}", f"jira:{key}", f"{key} — {summary}", "jira", BROWSE.format(key=key),
                         "Jira AP", "Welcome Jira", mod(80 + i), [body]))
    # GitHub, dbt, Power BI, uploads.
    docs.append(_doc("pr812", "github:pr:812", "Add booking cancellation flags to fct_bookings (PR #812)", "github",
                     f"{GH}/pull/812", "GitHub dbt-analytics", "Welcome GitHub", mod(90),
                     [_body(rng, "", ["booking", "cancellation", "model", "column", "schema"], 2)]))
    docs.append(_doc("dbt1", "dbt:models/marts/fct_transfers.yml", "fct_transfers model definitions", "dbt",
                     f"{GH}/blob/main/models/marts/fct_transfers.yml", "dbt models", "Welcome GitHub", mod(91),
                     [_body(rng, "", ["model", "measure", "column", "dataset", "transfer"], 3, list_items=6)]))
    docs.append(_doc("dbt2", "dbt:models/staging/stg_partners.yml", "stg_partners staging model", "dbt", "",
                     "dbt models", "Welcome GitHub", mod(92),
                     [_body(rng, "", ["partner", "model", "column", "schema"], 2)]))
    docs.append(_doc("pbi1", "powerbi:finance:gmv", "Power BI measures: Finance GMV (08. Finance)", "powerbi", "",
                     "Power BI Finance", "Welcome Power BI", None,
                     [_body(rng, "", ["measure", "revenue", "margin", "commission", "dataset"], 3, table_rows=12)]))
    for i, name in enumerate(("Partner contract renewal checklist", "Airport greeting procedure")):
        docs.append(_doc(f"up{i}", f"upload:{name}", name, "upload", "", "Uploads", "Uploaded documents",
                         mod(95 + i), [_body(rng, "", ["partner", "contract", "renewal", "airport", "greeting",
                                                       "procedure", "checklist"], 3, list_items=7, tiny=1)]))
    from ingest.redact import redact_text
    for d in docs:
        for p in d["pages"]:
            assert redact_text(p, [])[0] == p, d["key"]
            assert p == p.strip(), d["key"]
        assert redact_text(d["title"], [])[0] == d["title"]
    return docs


def multi_page_doc() -> dict:
    """An upload with three physical pages (a PDF): DataGov chunks each page on its own, so
    luccy (one body per page file) would chunk it differently. Served live, never compared."""
    rng = random.Random(99)
    topic = ["luggage", "delay", "complaint", "voucher", "airport"]
    pages = [_body(rng, "", topic, 2) for _ in range(3)]
    return _doc("pdf3", "upload:lost luggage policy.pdf", "Lost luggage policy", "upload", "", "Uploads",
                "Uploaded documents", "2026-05-05T08:00:00+00:00", pages)


QUERIES = [
    # (query, sources) -- sources None = all on
    ("refund after booking cancellation", None),
    ("driver payout delayed weekend", None),
    ("AP-520", None),
    ("PHOENIX-1724 dispatch allocation airport", None),
    ("upsell attribution CRM channel", None),
    ("quarterly business review revenue target", None),
    ("QBR Q1 2024 partner forecast", None),
    ("monthly KPI report template", None),
    ("dispatch allocation fleet capacity Athens", None),
    ("regional operations handbook Lisbon pickup", None),
    ("fct_transfers model column definitions", None),
    ("booking cancellation flags dbt pull request", None),
    ("Power BI GMV measure commission margin", None),
    ("partner contract renewal checklist", None),
    ("airport greeting procedure steps", None),
    ("complete manual volume escalation procedure", None),
    ("voucher discount applied twice", None),
    ("driver onboarding handbook training quality", None),
    ("the of and a to", None),
    ("zzqxv unknownterm", None),
    ("Seville Q3 revenue table", None),
    ("refund after booking cancellation", frozenset({"confluence"})),
    ("driver payout delayed weekend", frozenset({"jira"})),
    ("partner contract renewal checklist", frozenset({"drive", "github"})),
    ("dispatch allocation fleet capacity Athens", frozenset({"confluence", "drive", "github"})),
    ("fct_transfers model column definitions", frozenset({"confluence", "jira"})),
    ("Power BI GMV measure commission margin", frozenset({"jira"})),
]

# Questions run through luccy's whole driver to the writer, with the planner's sub-queries.
PLANS = {
    "Why was the refund not issued after the cancellation, and what does the policy say?":
        ["refund not issued after cancellation", "refund policy cancellation"],
    "Compare the quarterly business reviews on revenue and targets.":
        ["quarterly business review revenue", "QBR targets partner forecast", "region revenue quarter"],
    "How do dispatch and allocation work across the regional operations handbooks?":
        ["dispatch allocation fleet capacity", "regional operations handbook driver pickup",
         "airport pickup operations capacity", "fleet allocation weekend demand"],
    "Walk me through the partner contract renewal checklist.":
        ["partner contract renewal checklist", "contract renewal procedure steps"],
    "What does the complete manual say about escalation and audits?":
        ["complete manual escalation procedure", "audit quality checklist"],
    "Which dbt models define transfers and booking cancellation flags?":
        ["fct_transfers model column definitions", "booking cancellation flags pull request"],
    "What is in the monthly KPI report template?":
        ["monthly KPI report template", "report metric dashboard target"],
    "Who handles driver payout delays on weekends?":
        ["driver payout delayed weekend", "payout invoice supplier"],
}
