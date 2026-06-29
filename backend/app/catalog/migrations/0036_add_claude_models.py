from django.db import migrations


OPUS = 'anthropic:claude-opus-4-8'
HAIKU = 'anthropic:claude-haiku-4-5'


def add_claude_models(apps, schema_editor):
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    Organization = apps.get_model('catalog', 'Organization')

    seeds = [
        {
            'identifier': OPUS,
            'display_name': 'Claude Opus 4.8',
            'sort_order': 5,
        },
        {
            'identifier': HAIKU,
            'display_name': 'Claude Haiku 4.5 (Fast)',
            'sort_order': 7,
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

    # Switch the AI Assistant from Google to Claude: repoint every org still on
    # a Gemini model (or with no model set) to Claude Opus 4.8, the new project
    # default. Orgs that have explicitly picked some other model are left alone.
    opus = ChatbotModel.objects.filter(identifier=OPUS).first()
    if opus is not None:
        Organization.objects.filter(
            chatbot_model__isnull=True
        ).update(chatbot_model=opus)
        Organization.objects.filter(
            chatbot_model__identifier__startswith='google-gla:'
        ).update(chatbot_model=opus)


def remove_claude_models(apps, schema_editor):
    # Reverse: drop the seeded Claude models. Orgs pointing at them fall back to
    # NULL (on_delete=SET_NULL) and then to the project DEFAULT_CHATBOT_MODEL.
    ChatbotModel = apps.get_model('catalog', 'ChatbotModel')
    ChatbotModel.objects.filter(identifier__in=[OPUS, HAIKU]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0035_governancetask_assignee_role'),
    ]

    operations = [
        migrations.RunPython(add_claude_models, remove_claude_models),
    ]
