"""The Document Analyzer replaced DataGov's own document search as the assistant's
documents tool: the "documents" tool group is gone and "analyzer" is the documents tool.

A member blocked from (or who switched off) "documents" stays without documents: a
False "documents" becomes False "analyzer" in the same field unless that field already
says something about "analyzer". The "documents" key is then removed from both fields.
"""
from django.db import migrations

FIELDS = ('assistant_tool_access', 'assistant_tool_preferences')


def documents_to_analyzer(apps, schema_editor):
    Membership = apps.get_model('catalog', 'OrganizationMembership')
    for membership in Membership.objects.only('pk', *FIELDS).iterator():
        changed = []
        for field in FIELDS:
            value = getattr(membership, field)
            if not isinstance(value, dict) or 'documents' not in value:
                continue
            value = dict(value)
            if value.pop('documents') is False and 'analyzer' not in value:
                value['analyzer'] = False
            setattr(membership, field, value)
            changed.append(field)
        if changed:
            membership.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [('catalog', '0068_member_assistant_tools')]
    operations = [migrations.RunPython(documents_to_analyzer, migrations.RunPython.noop)]
