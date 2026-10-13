from django.db import migrations, models
import django.db.models.deletion


def bind_unambiguous_sessions(apps, schema_editor):
    Membership = apps.get_model('catalog', 'OrganizationMembership')
    Session = apps.get_model('catalog', 'ChatSession')
    alias = schema_editor.connection.alias
    memberships = Membership.objects.using(alias).values('user_id').annotate(
        count=models.Count('organization_id'), org_id=models.Min('organization_id'),
    ).filter(count=1)
    for membership in memberships.iterator():
        Session.objects.using(alias).filter(
            user_id=membership['user_id'], organization__isnull=True,
        ).update(organization_id=membership['org_id'])


class Migration(migrations.Migration):
    dependencies = [('catalog', '0066_customuser_google_subject')]

    operations = [
        migrations.AddField(
            model_name='chatsession', name='organization',
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='chat_sessions', to='catalog.organization'),
        ),
        migrations.AddField(
            model_name='chatsession', name='mode',
            field=models.CharField(choices=[('catalog', 'Catalog'),
                ('documents', 'Document Explorer')], default='catalog', max_length=16),
        ),
        migrations.AddField(
            model_name='chatmessage', name='citations',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='organization', name='slack_team_id',
            field=models.CharField(blank=True, null=True, unique=True, max_length=80),
        ),
        migrations.AddField(
            model_name='dataperson', name='slack_user_id',
            field=models.CharField(blank=True, default='', max_length=80,
                help_text='Verified Slack member ID for this organization, e.g. U0123456789.'),
        ),
        migrations.AddConstraint(
            model_name='dataperson',
            constraint=models.UniqueConstraint(fields=('organization', 'slack_user_id'),
                condition=~models.Q(slack_user_id=''), name='uniq_dataperson_slack_org'),
        ),
        migrations.RunPython(bind_unambiguous_sessions, migrations.RunPython.noop),
    ]
