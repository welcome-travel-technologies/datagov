"""Standalone sanity check for the new composite-id graph model.

Run from the `app` directory:
    python scripts/_check_network.py

Prints counts, duplicate detection, and sample ego graph rows so we can
verify the fix without booting the full Django dev server.
"""
import os
import sys
import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from catalog.models import NetworkNode, NetworkEdge, Item  # noqa: E402
from django.db.models import Count  # noqa: E402

print("=" * 60)
print("NETWORK TABLE COUNTS")
print("=" * 60)
print(f"  NetworkNode rows: {NetworkNode.objects.count()}")
print(f"  NetworkEdge rows: {NetworkEdge.objects.count()}")
print(f"  Item rows:        {Item.objects.count()}")

print()
print("=" * 60)
print("UNIQUENESS CHECK")
print("=" * 60)
total = NetworkNode.objects.count()
distinct = NetworkNode.objects.values("node_id").distinct().count()
print(f"  distinct node_id vs total: {distinct} / {total}  -> {'OK' if distinct == total else 'DUPLICATES FOUND'}")

dup = (NetworkNode.objects.values("node_id")
       .annotate(n=Count("node_id")).filter(n__gt=1)[:5])
if dup:
    print("  Sample duplicates:", list(dup))

print()
print("=" * 60)
print("NODE TYPE BREAKDOWN")
print("=" * 60)
for grp in NetworkNode.objects.values("group").annotate(n=Count("node_id")).order_by("-n"):
    print(f"  {grp['group'] or 'UNKNOWN':<10} {grp['n']}")

print()
print("=" * 60)
print("SAMPLE NODES (first 5)")
print("=" * 60)
for n in NetworkNode.objects.all()[:5]:
    print(f"  node_id={n.node_id!r}  name={n.name!r}  group={n.group!r}")

print()
print("=" * 60)
print("NAMES SHARED ACROSS MULTIPLE TYPES (the bug scenario)")
print("=" * 60)
name_dupes = (NetworkNode.objects.values("name")
              .annotate(n=Count("node_id"))
              .filter(n__gt=1)
              .order_by("-n")[:10])
if name_dupes:
    for row in name_dupes:
        examples = list(NetworkNode.objects.filter(name=row["name"])
                        .values("node_id", "group")[:5])
        print(f"  {row['name']!r} appears {row['n']} times: {examples}")
else:
    print("  (none — all names are unique; the bug is impossible in this dataset)")

print()
print("=" * 60)
print("ITEM vs NODE JOIN HEALTH")
print("=" * 60)
# For each catalog-resident node type, the hash after "::" should exist in Item.item_id.
sample_types = ("PB_TABLE", "PB_COLUMN", "PB_MEASURE", "PB_REPORT", "PB_PAGE", "PB_VISUAL", "PB_FIELD")
for t in sample_types:
    total_t = NetworkNode.objects.filter(group=t).count()
    if not total_t:
        continue
    sample = NetworkNode.objects.filter(group=t).first()
    hash_part = sample.node_id.split("::", 1)[1] if "::" in sample.node_id else sample.node_id
    exists = Item.objects.filter(item_id=hash_part).exists()
    print(f"  {t:<8} count={total_t:<5} sample node_id={sample.node_id} "
          f"matches Item? {'yes' if exists else 'NO'}")

print()
print("All checks done.")
