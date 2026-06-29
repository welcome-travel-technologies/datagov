import json
import sys
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from slack_sdk import WebClient
from pydantic_ai.messages import ModelRequest, UserPromptPart, ModelResponse, TextPart
from django.conf import settings
from .decorators import api_login_required
from .models import OrganizationMembership
from .models import Organization
from .views import build_chatbot_agent_for_org
from slack_blocks_markdown import markdown_to_blocks
from django_q.tasks import async_task

def run_slack_event_sync(event_data, bot_token):
    """
    Module-level function required by Django Q to run background tasks.
    Runs synchronously using WebClient instead of AsyncWebClient.

    Returns the assistant's reply text so Django-Q stores it on
    ``Task.result`` — that is what surfaces in Org Settings → Queues, so an
    admin can read what the bot actually answered (or why it failed). On error
    we log the traceback and return an error string rather than re-raising, so
    Django-Q does not retry and post a duplicate reply into the Slack thread.
    """
    try:
        return process_slack_event(event_data, bot_token)
    except Exception as e:
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        return f"⚠️ Slack reply failed: {e}"

def process_slack_event(event_data, bot_token):
    client = WebClient(token=bot_token)
    channel_id = event_data.get('channel')
    # By passing the original event's ts as thread_ts, we ensure the bot ALWAYS replies in a thread.
    # If the user already messaged in a thread, thread_ts will be set and we keep appending.
    # If they just mentioned in the main channel, thread_ts will be their message's ts, so the bot creates a thread.
    thread_ts = event_data.get('thread_ts') or event_data.get('ts')

    # Get bot info to identify its own messages
    auth_info = client.auth_test()
    bot_user_id = auth_info['user_id']

    # Fetch thread history
    # Include groups:history equivalent by using the standard conversations_replies endpoint
    # which works for public/private channels and DMs automatically as long as the scopes are present
    history_response = client.conversations_replies(
        channel=channel_id,
        ts=thread_ts,
    )

    messages = history_response.get('messages', [])
    formatted_history = []

    for msg in messages[:-1]:  # Exclude the latest message
        text = msg.get('text', '')
        if not text:
            continue

        # If the message was from the bot
        if msg.get('user') == bot_user_id or msg.get('bot_id'):
            formatted_history.append(ModelResponse(parts=[TextPart(content=text)]))
        else:
            formatted_history.append(ModelRequest(parts=[UserPromptPart(content=text)]))

    new_message = messages[-1].get('text', '')

    # Strip mention from new message if needed
    mention_str = f"<@{bot_user_id}>"
    if new_message.startswith(mention_str):
        new_message = new_message.replace(mention_str, '', 1).strip()

    org = Organization.objects.filter(slack_bot_token=bot_token).first()
    debug_log: list = []

    # Same context-first agent as the web chat: front-loaded catalog +
    # per-integration schema/run tools (PowerBI live values via
    # get_pb_measure_schema → powerbi_run_dax_query). The Slack
    # thread is the memory — prior turns arrive via ``formatted_history``.
    agent = build_chatbot_agent_for_org(
        org,
        record_call=debug_log.append,
        surface='slack',
        chat_session=None,
    )
    from .views import retry_transient_llm_errors
    result = retry_transient_llm_errors(
        lambda: agent.run_sync(user_prompt=new_message, message_history=formatted_history)
    )
    output_text = result.output

    if org and org.debug_responses_enabled:
        from .services.debug_render import build_debug_payload, render_debug_section
        output_text = (output_text or '') + render_debug_section(build_debug_payload(debug_log))

    # Pre-process: Slack only allows 1 table per message in Block Kit.
    # We split the message so each table goes into its own message,
    # but keep trailing text attached to the final table.
    import re
    table_pattern = r'((?:^\|.*?\|\s*?(?:\n|$))+)'
    parts = re.split(table_pattern, output_text, flags=re.MULTILINE)

    total_tables = 0
    for i, part in enumerate(parts):
        if i % 2 == 1 and re.search(r'\|[\-\s:]+\|', part):
            total_tables += 1

    messages_to_send = []
    current_message = ""
    tables_seen = 0

    for i, part in enumerate(parts):
        is_match = (i % 2 == 1)
        is_real_table = False

        if is_match:
            if re.search(r'\|[\-\s:]+\|', part):
                is_real_table = True

        current_message += part

        if is_real_table:
            tables_seen += 1
            if tables_seen < total_tables:
                if current_message.strip():
                    messages_to_send.append(current_message.strip())
                current_message = ""

    if current_message.strip():
        messages_to_send.append(current_message.strip())

    # Reply with each chunk
    for msg_text in messages_to_send:
        # Use slack-blocks-markdown to convert Markdown response directly into interactive Block Kit blocks
        blocks = markdown_to_blocks(msg_text)

        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Assistant replied:", # Fallback text
            blocks=blocks
        )

    # Returned to Django-Q as Task.result so the reply is visible in the
    # Queues panel. Fall back to a marker when the agent produced no text.
    return output_text or "(no reply text)"

@csrf_exempt
def slack_events(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
        
    try:
        data = json.loads(request.body)
    except Exception:
        return HttpResponse(status=400)
        
    if data.get('type') == 'url_verification':
        return JsonResponse({'challenge': data.get('challenge')})
        
    if data.get('type') == 'event_callback':
        event = data.get('event', {})
        event_type = event.get('type')
        
        if event_type in ['app_mention', 'message']:
            # Ignore bot's own messages
            if event.get('bot_id') or event.get('subtype') == 'bot_message':
                return HttpResponse(status=200)

            # Take the first valid token
            org = Organization.objects.filter(slack_bot_token__isnull=False).exclude(slack_bot_token='').first()
            
            if org and org.slack_bot_token:
                # Process async to avoid 3s timeout. Using Django Q2 worker queue.
                async_task('catalog.slack_views.run_slack_event_sync', event, org.slack_bot_token)
                
    return HttpResponse(status=200)


@api_login_required
def slack_oauth(request):
    """
    Handles the OAuth callback for the Slack Bot (app_mentions:read + chat:write).
    """
    return _handle_slack_oauth(request, hook_type='slack', redirect_name='integrations')


@api_login_required
def slack_alerts_oauth(request):
    """
    Handles the OAuth callback for Slack Alerts (chat:write only).
    """
    return _handle_slack_oauth(request, hook_type='slack_alerts', redirect_name='integrations')


def _handle_slack_oauth(request, hook_type, redirect_name):
    code = request.GET.get('code')
    error = request.GET.get('error')

    if error:
        return HttpResponse(f"Error during Slack authentication: {error}")

    if not code:
        return HttpResponse("No code provided by Slack.")

    client = WebClient()

    try:
        redirect_uri = f"https://{request.get_host()}/api/slack/oauth/" if hook_type == 'slack' else f"https://{request.get_host()}/api/slack/alerts-oauth/"
        response = client.oauth_v2_access(
            client_id=settings.SLACK_CLIENT_ID,
            client_secret=settings.SLACK_CLIENT_SECRET,
            code=code,
            redirect_uri=redirect_uri
        )

        bot_token = response.get("access_token")

        memberships = OrganizationMembership.objects.filter(user=request.user, is_admin=True)
        if memberships.exists():
            org = memberships.first().organization

            from .models import IntegrationHook
            hook, _ = IntegrationHook.objects.get_or_create(
                organization=org,
                hook_type=hook_type,
                defaults={'name': 'Slack Bot' if hook_type == 'slack' else 'Slack Alerts'}
            )
            hook.slack_bot_token = bot_token
            hook.is_active = True
            hook.save()

            # Keep Organization.slack_bot_token in sync for bot hook (used by event handler)
            if hook_type == 'slack':
                org.slack_bot_token = bot_token
                org.save(update_fields=['slack_bot_token'])

            return HttpResponse(
                "Slack connected successfully. You can close this window and "
                "return to the Integrations page."
            )
        else:
            return HttpResponse("You don't have admin rights to any organization.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        return HttpResponse(f"Failed to authenticate with Slack: {str(e)}")
