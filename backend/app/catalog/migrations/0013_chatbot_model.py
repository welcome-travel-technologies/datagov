from django.db import migrations, models


def seed_chatbot_models(apps, schema_editor):
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    Organization = apps.get_model('catalog', 'Organization')

    seeds = [
        {
            'identifier': 'google-gla:gemini-3.1-pro-preview',
            'display_name': 'Gemini 3.1 Pro (Preview)',
            'sort_order': 10,
        },
        {
            'identifier': 'google-gla:gemini-3-flash-preview',
            'display_name': 'Gemini 3 Flash (Preview)',
            'sort_order': 20,
        },
    ]
    for seed in seeds:
        ChatbotModel.objects.update_or_create(
            identifier=seed['identifier'],
            defaults={
                'display_name': seed['display_name'],
                'sort_order': seed['sort_order'],
                'is_active': True,
            },
        )

    # Default existing orgs to the Pro model so the chatbot keeps working.
    default = ChatbotModel.objects.filter(
        identifier='google-gla:gemini-3.1-pro-preview'
    ).first()
    if default is not None:
        Organization.objects.filter(chatbot_model__isnull=True).update(
            chatbot_model=default
        )


def unseed_chatbot_models(apps, schema_editor):
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    ChatbotModel.objects.filter(
        identifier__in=[
            'google-gla:gemini-3.1-pro-preview',
            'google-gla:gemini-3-flash-preview',
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0012_rename_dbt_column'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatbotModel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('identifier', models.CharField(max_length=200, unique=True)),
                ('display_name', models.CharField(max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
            ],
            options={
                'ordering': ['sort_order', 'display_name'],
            },
        ),
        migrations.AddField(
            model_name='organization',
            name='chatbot_model',
            field=models.ForeignKey(
                blank=True,
                help_text='Which LLM the AI Assistant uses to answer questions.',
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='organizations',
                to='catalog.chatbotmodel',
            ),
        ),
        migrations.RunPython(seed_chatbot_models, unseed_chatbot_models),
    ]
