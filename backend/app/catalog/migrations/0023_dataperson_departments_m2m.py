from django.db import migrations, models


def copy_fk_to_m2m(apps, schema_editor):
    """Move existing single-FK department links into the new M2M table.

    Runs after the M2M field exists and before the old FK is dropped, so
    each DataPerson keeps its current department as the first entry in
    its departments list.
    """
    DataPerson = apps.get_model('catalog', 'DataPerson')
    for p in DataPerson.objects.exclude(department__isnull=True).iterator():
        p.departments.add(p.department_id)


def copy_m2m_to_fk(apps, schema_editor):
    """Reverse: pick an arbitrary department from the M2M as the single FK."""
    DataPerson = apps.get_model('catalog', 'DataPerson')
    for p in DataPerson.objects.iterator():
        first = p.departments.first()
        if first and not p.department_id:
            p.department = first
            p.save(update_fields=['department'])


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0022_owner_to_dataperson'),
    ]

    operations = [
        # 1. First, free up the related_name='data_persons' name on the old
        #    FK so the new M2M can claim it. Without this, AddField below
        #    would fail Django's system check (clash on DataPerson.data_persons).
        migrations.AlterField(
            model_name='dataperson',
            name='department',
            field=models.ForeignKey(
                null=True,
                on_delete=models.SET_NULL,
                related_name='+',
                to='catalog.department',
            ),
        ),

        # 2. Add the new M2M field with the reclaimed related_name.
        migrations.AddField(
            model_name='dataperson',
            name='departments',
            field=models.ManyToManyField(
                blank=True,
                related_name='data_persons',
                to='catalog.department',
            ),
        ),

        # 3. Backfill: every DataPerson's old single department becomes the
        #    first entry in its new M2M list.
        migrations.RunPython(copy_fk_to_m2m, copy_m2m_to_fk),

        # 4. Drop the now-redundant single FK.
        migrations.RemoveField(
            model_name='dataperson',
            name='department',
        ),
    ]
