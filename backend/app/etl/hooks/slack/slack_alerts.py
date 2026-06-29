def send_slack_alert(source, run_log):
    """
    Send a Slack alert to the configured alerts channel after a source run completes.
    Uses the slack_alerts hook (chat:write scope only).
    """
    try:
        from catalog.models import IntegrationHook
        hook = IntegrationHook.objects.filter(
            organization=source.organization,
            hook_type='slack_alerts',
            is_active=True,
        ).first()

        if not hook or not hook.slack_bot_token:
            return

        channel = hook.slack_alerts_channel or hook.slack_channel
        if not channel:
            return

        from slack_sdk import WebClient
        client = WebClient(token=hook.slack_bot_token)

        status_emoji = '✅' if run_log.status == 'success' else '❌'
        duration = ''
        if run_log.finished_at and run_log.started_at:
            secs = int((run_log.finished_at - run_log.started_at).total_seconds())
            duration = f'{secs}s'

        text = (
            f"{status_emoji} *Source run {run_log.status}*\n"
            f"*Source:* {source.name}\n"
            f"*Triggered by:* {run_log.triggered_by}\n"
            f"*Duration:* {duration or 'N/A'}\n"
            f"*Started:* {run_log.started_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )

        client.chat_postMessage(channel=channel, text=text)
    except Exception as e:
        print(f'[Slack alert] Failed to send source alert: {e}')


def send_slack_dest_alert(dest, status, duration_secs=None):
    """
    Send a Slack alert after a BigQuery destination push completes.
    """
    try:
        from catalog.models import IntegrationHook
        hook = IntegrationHook.objects.filter(
            organization=dest.organization,
            hook_type='slack_alerts',
            is_active=True,
        ).first()

        if not hook or not hook.slack_bot_token:
            return

        channel = hook.slack_alerts_channel or hook.slack_channel
        if not channel:
            return

        from slack_sdk import WebClient
        client = WebClient(token=hook.slack_bot_token)

        status_emoji = '✅' if status == 'success' else '❌'
        duration = f' in {duration_secs}s' if duration_secs is not None else ''

        text = (
            f"{status_emoji} *Destination push {status}*\n"
            f"*Destination:* {dest.name} (BigQuery)\n"
            f"*Dataset:* {dest.bq_dataset_id or 'N/A'}{duration}"
        )

        client.chat_postMessage(channel=channel, text=text)
    except Exception as e:
        print(f'[Slack alert] Failed to send destination alert: {e}')


def send_slack_item_alert(item, user, change_type, old_value, new_value):
    """
    Slack alert for Item status or `deleted` flag change.
    change_type: 'status' or 'deleted'
    """
    try:
        if not item or not getattr(item, 'organization_id', None):
            return
        from catalog.models import IntegrationHook
        hook = IntegrationHook.objects.filter(
            organization_id=item.organization_id,
            hook_type='slack_alerts',
            is_active=True,
        ).first()
        if not hook or not hook.slack_bot_token:
            return
        channel = hook.slack_alerts_channel or hook.slack_channel
        if not channel:
            return

        from slack_sdk import WebClient
        client = WebClient(token=hook.slack_bot_token)

        who = (getattr(user, 'email', None) or getattr(user, 'username', None) or 'system')
        ctx = f"{item.workspace_name or '—'} / {item.dataset_name or '—'} / {item.table_name or '—'}"
        label = f"*{item.item_name or item.item_id}*"
        link_suffix = f"\n<{item.web_url}|Open in Power BI>" if item.web_url else ''

        if change_type == 'status':
            emoji, title = '🔔', f'{item.item_type or "Item"} status changed'
            body = (f"{label}\n`{ctx}`\n"
                    f"Status: `{old_value or '—'}` → `{new_value or '—'}`\nChanged by: {who}")
        elif change_type == 'deleted' and new_value:
            emoji, title = '🗑️', f'{item.item_type or "Item"} marked for deletion'
            body = f"{label}\n`{ctx}`\nMarked by: {who}"
        else:
            return

        client.chat_postMessage(channel=channel, text=f"{emoji} *{title}*\n{body}{link_suffix}")
    except Exception as e:
        print(f'[Slack alert] Failed to send item alert: {e}')


def send_slack_task_alert(task):
    """
    Slack alert announcing a new/updated governance task.

    Tags the assignee via their `slack_handle` text when set (Slack only renders
    a true <@user> mention from a user ID, which we don't store — the handle
    string is the best available). Falls back to the assignee's name, or no
    assignee line when the task is unassigned.
    """
    try:
        if not task or not getattr(task, 'organization_id', None):
            return
        from catalog.models import IntegrationHook
        hook = IntegrationHook.objects.filter(
            organization_id=task.organization_id,
            hook_type='slack_alerts',
            is_active=True,
        ).first()
        if not hook or not hook.slack_bot_token:
            return
        channel = hook.slack_alerts_channel or hook.slack_channel
        if not channel:
            return

        from slack_sdk import WebClient
        client = WebClient(token=hook.slack_bot_token)

        grp = task.item_group
        rep = (grp.primary_item or grp.items.first()) if grp else None
        if rep is not None:
            ctx = f"{rep.workspace_name or '—'} / {rep.dataset_name or '—'} / {rep.table_name or '—'}"
        else:
            ctx = '—'

        lines = [
            f"📋 *New governance task*",
            f"*{task.title}*",
            f"`{ctx}`",
            f"Status: `{task.trigger_status}`",
        ]
        assignee = task.assignee
        role = (task.assignee_role or 'assignee').capitalize()
        if assignee and assignee.slack_handle:
            lines.append(f"{role}: {assignee.slack_handle}")
        elif assignee:
            lines.append(f"{role}: {assignee.name}")

        client.chat_postMessage(channel=channel, text="\n".join(lines))
    except Exception as e:
        print(f'[Slack alert] Failed to send task alert: {e}')
