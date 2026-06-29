# Consistent assistant-tool naming across integrations:
#   <integration>_tools_enabled       → CATALOG tier (read-only)
#   <integration>_live_tools_enabled  → LIVE-execution tier (default OFF)
#
# The existing *_tools_enabled fields gated LIVE execution, so they are renamed
# to *_live_tools_enabled (preserving each org's value), and fresh catalog-tier
# *_tools_enabled fields are added. Only the PowerBI catalog tier ships ON by
# default; the dbt and BigQuery catalog tiers default OFF (opt-in), as do all
# live tiers. Existing rows keep their stored value.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0052_organization_chat_timeout_seconds'),
    ]

    operations = [
        # 1. Rename the existing live flags (frees the *_tools_enabled names).
        migrations.RenameField(
            model_name='organization',
            old_name='powerbi_tools_enabled',
            new_name='powerbi_live_tools_enabled',
        ),
        migrations.RenameField(
            model_name='organization',
            old_name='bigquery_tools_enabled',
            new_name='bigquery_live_tools_enabled',
        ),
        # 2. Set the renamed live fields' final help_text.
        migrations.AlterField(
            model_name='organization',
            name='powerbi_live_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Allow the AI Assistant to run live DAX queries against '
                          'the PowerBI REST API.',
            ),
        ),
        migrations.AlterField(
            model_name='organization',
            name='bigquery_live_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Allow the AI Assistant to run read-only live SQL queries '
                          'against BigQuery.',
            ),
        ),
        # 3. Add the catalog-tier flags. Only PowerBI ships ON by default;
        #    BigQuery's catalog tier defaults OFF (opt-in).
        migrations.AddField(
            model_name='organization',
            name='powerbi_tools_enabled',
            field=models.BooleanField(
                default=True,
                help_text='PowerBI catalog assistant: front-load the measure/report '
                          'listing and register the read-only profiler (get_pb_item_details) '
                          '+ usage-analytics (get_pb_usage_analytics) tools. Local catalog '
                          'DB only — no external calls.',
            ),
        ),
        migrations.AddField(
            model_name='organization',
            name='bigquery_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='BigQuery catalog assistant: load the in-scope dataset schema '
                          '(tables, columns, types) into context, read-only. No query '
                          'execution.',
            ),
        ),
        # 4. dbt stays catalog-only; keep its default OFF (opt-in) and refresh
        #    the help_text.
        migrations.AlterField(
            model_name='organization',
            name='dbt_tools_enabled',
            field=models.BooleanField(
                default=False,
                help_text='dbt catalog assistant: front-load the model/column listing and '
                          'register the dbt profiler + lineage tools. Local catalog DB only '
                          '(dbt has no live-query tier).',
            ),
        ),
    ]
