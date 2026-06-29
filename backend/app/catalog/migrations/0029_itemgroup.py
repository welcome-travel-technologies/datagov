"""
Introduce ItemGroup — governance moves off Item onto a group.

* Every Item gets exactly one ItemGroup:
  - PB_MEASURE rows sharing a name/group_id collapse into ONE
    ``kind='measure_name'`` group (governance curated once, shared).
  - Everything else gets its own ``kind='singleton'`` group.
* Governance (ownership_department/person, steward, category, status,
  custom_description) is copied UP to the group using the same
  "pinned/heuristic primary wins, else first non-empty; status prefers
  curated" rule the Data Dictionary already used, so no curation is lost.
* ``ItemGroup.primary_item`` is set to the pinned (old is_group_primary) or
  heuristic representative — it drives the default workspace/dataset/DAX.
* The old per-Item governance columns are removed from Django STATE only
  (SeparateDatabaseAndState) — the DB columns stay as a deprecated safety
  net and get physically dropped in a later migration.
* ``is_group_primary`` (added by 0027, never deployed) is fully dropped:
  the backfill consumes it into ``primary_item``.

App downtime during this migration is expected and acceptable.
"""
import re

import django.db.models.deletion
from django.db import migrations, models


_EXT_RE = re.compile(r'external\s*measure')

_STATUS_CHOICES = [
    ('UNVERIFIED', 'Unverified'),
    ('VERIFIED', 'Verified'),
    ('DEPRECATED', 'Deprecated'),
    ('ATTENTION', 'Attention'),
]


def _is_external(expr, desc):
    e = (expr or '').strip()
    if not e:
        return True
    return bool(_EXT_RE.search((e + ' ' + (desc or '')).lower()))


def _ws_priority(name):
    w = (name or '').lower()
    if 'finance' in w:
        return 0
    if 'commercial' in w:
        return 1
    if 'ops' in w or 'operation' in w:
        return 2
    if 'marketing' in w:
        return 3
    return 4


def _sort_key(rec):
    # Pinned (old is_group_primary) first, then the Data Dictionary heuristic:
    # non-external, workspace priority, dataset name, item_id.
    return (
        0 if rec['is_group_primary'] else 1,
        1 if _is_external(rec['expression'], rec['description']) else 0,
        _ws_priority(rec['workspace_name']),
        rec['dataset_name'] or '',
        rec['item_id'] or '',
    )


def forwards(apps, schema_editor):
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')

    fields = [
        'item_id', 'item_type', 'organization_id', 'workspace_name',
        'dataset_name', 'expression', 'description',
        'ownership_department_id', 'ownership_person_id', 'steward_id',
        'category_id', 'status', 'custom_description', 'is_group_primary',
        'group_id',
    ]

    buckets = {}   # group_key -> list[rec]
    meta = {}      # group_key -> (kind, org_id)
    for rec in Item.objects.all().values(*fields).iterator(chunk_size=2000):
        if rec['item_type'] == 'PB_MEASURE' and rec['group_id']:
            key, kind = rec['group_id'], 'measure_name'
        else:
            key, kind = f"item::{rec['item_id']}", 'singleton'
        buckets.setdefault(key, []).append(rec)
        if key not in meta:
            meta[key] = (kind, rec['organization_id'])

    resolved = {}   # group_key -> resolved governance + primary_item_id
    to_create = []
    for key, recs in buckets.items():
        kind, org_id = meta[key]
        ordered = sorted(recs, key=_sort_key)
        primary = ordered[0]

        def first(field):
            for r in ordered:
                if r[field]:
                    return r[field]
            return None

        st = primary['status']
        if not st or st == 'UNVERIFIED':
            st = next(
                (r['status'] for r in ordered
                 if r['status'] and r['status'] != 'UNVERIFIED'),
                'UNVERIFIED',
            )

        resolved[key] = {
            'ownership_department_id': first('ownership_department_id'),
            'ownership_person_id': first('ownership_person_id'),
            'steward_id': first('steward_id'),
            'category_id': first('category_id'),
            'custom_description': first('custom_description'),
            'status': st or 'UNVERIFIED',
            'primary_item_id': primary['item_id'],
        }
        to_create.append(ItemGroup(
            group_key=key, kind=kind, organization_id=org_id,
            ownership_department_id=resolved[key]['ownership_department_id'],
            ownership_person_id=resolved[key]['ownership_person_id'],
            steward_id=resolved[key]['steward_id'],
            category_id=resolved[key]['category_id'],
            custom_description=resolved[key]['custom_description'],
            status=resolved[key]['status'],
        ))

    ItemGroup.objects.bulk_create(to_create, batch_size=1000)
    gmap = dict(ItemGroup.objects.values_list('group_key', 'id'))

    # Link every item to its group (only item_group_id is written).
    item_updates = []
    for key, recs in buckets.items():
        gpk = gmap[key]
        for r in recs:
            item_updates.append(Item(item_id=r['item_id'], item_group_id=gpk))
    Item.objects.bulk_update(item_updates, ['item_group'], batch_size=1000)

    # Now that items are linked, point each group at its primary item.
    grp_updates = [
        ItemGroup(id=gmap[key], primary_item_id=resolved[key]['primary_item_id'])
        for key in buckets
    ]
    ItemGroup.objects.bulk_update(grp_updates, ['primary_item'], batch_size=1000)


def backwards(apps, schema_editor):
    Item = apps.get_model('catalog', 'Item')
    ItemGroup = apps.get_model('catalog', 'ItemGroup')
    # The 5 nullable governance columns were never overwritten (state-only
    # removal), so they still hold the original per-item values. Only `status`
    # was physically dropped — restore it from the group so a reverse is
    # lossless. (is_group_primary reverts to its default; rarely reversed.)
    for ig in ItemGroup.objects.all().values('id', 'status').iterator(chunk_size=2000):
        Item.objects.filter(item_group_id=ig['id']).update(status=ig['status'])
    Item.objects.update(item_group=None)
    ItemGroup.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0028_consolidate_access_groups'),
    ]

    operations = [
        migrations.CreateModel(
            name='ItemGroup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('group_key', models.CharField(max_length=1100, unique=True)),
                ('kind', models.CharField(choices=[('measure_name', 'Measure (grouped by name)'), ('singleton', 'Singleton')], default='singleton', max_length=20)),
                ('status', models.CharField(choices=_STATUS_CHOICES, default='UNVERIFIED', max_length=20)),
                ('custom_description', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('category', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='item_groups', to='catalog.category')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='item_groups', to='catalog.organization')),
                ('ownership_department', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owned_groups', to='catalog.department')),
                ('ownership_person', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owned_groups', to='catalog.dataperson')),
                ('steward', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='stewarded_groups', to='catalog.dataperson')),
                ('primary_item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.item')),
            ],
        ),
        migrations.AddIndex(
            model_name='itemgroup',
            index=models.Index(fields=['group_key'], name='cat_itemgroup_key_idx'),
        ),
        migrations.AddIndex(
            model_name='itemgroup',
            index=models.Index(fields=['kind'], name='cat_itemgroup_kind_idx'),
        ),
        migrations.AddIndex(
            model_name='itemgroup',
            index=models.Index(fields=['organization'], name='cat_itemgroup_org_idx'),
        ),
        migrations.AddField(
            model_name='item',
            name='item_group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='items', to='catalog.itemgroup', help_text="The ItemGroup that owns this item's governance."),
        ),
        migrations.RunPython(forwards, backwards),
        # The 5 NULLABLE governance fields: remove from Django STATE only —
        # keep the DB columns (deprecated safety net; physically dropped in a
        # later migration). They stay NULL-safe for ORM inserts.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='item', name='ownership_department'),
                migrations.RemoveField(model_name='item', name='ownership_person'),
                migrations.RemoveField(model_name='item', name='steward'),
                migrations.RemoveField(model_name='item', name='category'),
                migrations.RemoveField(model_name='item', name='custom_description'),
            ],
            database_operations=[],
        ),
        # `status` is NOT NULL with no DB default — keeping it as a dead column
        # would break every ORM Item insert. Hard-drop it; the value is safely
        # preserved in ItemGroup.status (and restored by backwards()).
        migrations.RemoveField(model_name='item', name='status'),
        # is_group_primary (0027, NOT NULL, never deployed) — hard-drop; the
        # backfill already folded it into ItemGroup.primary_item.
        migrations.RemoveField(model_name='item', name='is_group_primary'),
    ]
