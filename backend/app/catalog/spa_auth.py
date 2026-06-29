"""SPA session-auth endpoints for the React frontend.

Additive, JSON-only counterparts to Django's form login so the React app
(`welcome-data-catalog-react`) can reuse the existing session + RBAC. Served
under ``/api/`` alongside the rest of the API; the Next dev server proxies
``/api/*`` here so the session + csrftoken cookies flow on one origin.

  GET  /api/me/            -> current user + resolved can_view_* perms
  POST /api/auth/login/    -> authenticate + start a session ({username, password})
  POST /api/auth/logout/   -> end the session

Org-admin management (mirrors the classic Org Settings page + add-member wizard,
gated on the same ``can_view_org_settings`` / ``is_admin`` flags):

  GET  /api/org/members/        -> members + groups/departments/models/settings
  POST /api/org/members/save/   -> create or edit a member (account + profile + groups)
  POST /api/org/members/remove/ -> remove a member from the org (never yourself)
  POST /api/org/settings/       -> update bot feature-flags + display settings

Page-access flags are derived from the same `get_user_permissions` helper the
Django templates gate on (see catalog/access.py), so the SPA sidebar matches the
server-rendered app exactly.
"""
import json

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .access import ASSIGNABLE_GROUPS, is_org_admin, resolve_org
from .frontend_views import get_access_groups_qs, get_user_permissions
from .models import Organization, OrganizationMembership

# Every page-view flag the React sidebar reads (mirrors catalog/access.py keys).
_PERM_KEYS = [
    "is_admin",
    "can_view_dictionary",
    "can_view_tasks",
    "can_view_champions",
    "can_view_chat",
    "can_view_powerbi",
    "can_view_reports",
    "can_view_lineage",
    "can_view_unused",
    "can_view_insights",
    "can_view_dbt",
    "can_view_integrations",
    "can_view_org_settings",
]


def _me_payload(user):
    if not user.is_authenticated:
        return {"is_authenticated": False, "perms": {k: False for k in _PERM_KEYS}}

    perms = get_user_permissions(user)
    perms_out = {k: bool(perms[k]) for k in _PERM_KEYS}

    org = None
    mem = (
        OrganizationMembership.objects.filter(user=user)
        .select_related("organization")
        .first()
    )
    if mem and mem.organization:
        o = mem.organization
        org = {
            "name": getattr(o, "name", None),
            "primary_color": getattr(o, "primary_color", None),
            "icon": (o.icon.url if o.icon else None),
        }

    role = "admin" if (perms_out["is_admin"] or user.is_superuser) else "member"
    return {
        "id": user.id,
        "email": getattr(user, "email", "") or "",
        "username": getattr(user, "username", "") or "",
        "role": role,
        "is_authenticated": True,
        "perms": perms_out,
        "organization": org,
    }


@ensure_csrf_cookie
@require_GET
def me_view(request):
    """Current user + perms. Always 200 (guest payload when anonymous) and sets
    the csrftoken cookie so the SPA can POST to /auth/login/."""
    return JsonResponse(_me_payload(request.user))


@require_GET
def branding_view(request):
    """Public (no-auth) branding for the org: name, primary colour, and uploaded
    icon. Drives the login screen, favicon and page title before a session
    exists. Single-tenant in practice, so we return the first organization;
    authenticated views prefer the user's own org from ``/api/me/``."""
    o = Organization.objects.order_by("id").first()
    if not o:
        return JsonResponse({"name": None, "primary_color": None, "icon": None})
    return JsonResponse({
        "name": o.name,
        "primary_color": o.primary_color or None,
        "icon": (o.icon.url if o.icon else None),
    })


@require_POST
def login_view(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return JsonResponse({"error": "Username and password are required."}, status=400)

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"error": "Your credentials didn't match. Please try again."}, status=400)

    login(request, user)
    return JsonResponse(_me_payload(user))


@require_POST
def logout_view(request):
    logout(request)
    return JsonResponse({"ok": True})


@require_POST
def change_password_view(request):
    """Change the current user's password. JSON counterpart to the classic User
    Settings security tab — validates with Django's ``PasswordChangeForm`` (same
    fields: old_password, new_password1, new_password2) and keeps the session
    valid via ``update_session_auth_hash`` so the user isn't logged out."""
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    form = PasswordChangeForm(user=user, data=data)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    form.save()
    # Re-hash the session so the password change doesn't invalidate this session.
    update_session_auth_hash(request, user)
    return JsonResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Org-admin management (members + settings)
#
# JSON counterparts to the (now-removed) classic org-settings + add-member-wizard
# views. The write logic was ported faithfully — same validation, same
# group/DataPerson upserts, same self-removal guard — and then the React Org
# Settings page fully replaced the server-rendered admin flow.
# These are plain Django views (not DRF), so the CSRF middleware enforces the
# ``X-CSRFToken`` header the SPA already sends on unsafe methods.
# ---------------------------------------------------------------------------

def _admin_org(request):
    """Resolve (organization, error_response) for an org-admin request.

    Uses the unified ``access.is_org_admin`` predicate — the same gate the
    Integrations API and the page-visibility flags use — so org-admin means one
    thing everywhere. Returns ``(None, JsonResponse)`` on any failure.
    """
    user = request.user
    if not user.is_authenticated:
        return None, JsonResponse({"error": "Authentication required."}, status=401)

    org = resolve_org(user)
    if not (org and is_org_admin(user, org)):
        return None, JsonResponse(
            {"error": "Organization admin access required."}, status=403
        )
    return org, None


def _member_payload(membership, dp, request_user_id):
    """Serialize one OrganizationMembership (+ its DataPerson) for the table."""
    u = membership.user
    return {
        "user_id": u.id,
        "email": u.email,
        "username": u.username,
        "display_name": (dp.name if dp else "") or u.get_full_name() or u.email,
        "is_admin": membership.is_admin,
        "is_self": u.id == request_user_id,
        # only the self-service page-access groups (Company / Analytics);
        # org-admin is reported separately via is_admin, not as a group.
        "group_ids": [g.id for g in u.groups.all() if g.name in ASSIGNABLE_GROUPS],
        "is_owner": dp.is_owner if dp else False,
        "is_steward": dp.is_steward if dp else False,
        "is_other": dp.is_other if dp else False,
        "slack_handle": (dp.slack_handle if dp else "") or "",
        "department_ids": list(dp.departments.values_list("id", flat=True)) if dp else [],
    }


@require_GET
def org_members_view(request):
    """Everything the Org Settings page needs: members, the assignable groups,
    departments, chatbot models, and the org's feature-flag settings."""
    org, err = _admin_org(request)
    if err:
        return err

    from django.contrib.auth.models import Group
    from .models import ChatbotModel, DataPerson, Department

    # Make sure the self-service access groups exist (Admin is not a group).
    for name in ASSIGNABLE_GROUPS:
        Group.objects.get_or_create(name=name)

    memberships = (
        OrganizationMembership.objects.filter(organization=org)
        .select_related("user")
        .prefetch_related("user__groups")
        .order_by("user__email")
    )
    user_ids = [m.user_id for m in memberships]
    dps = {
        dp.user_id: dp
        for dp in DataPerson.objects.filter(user_id__in=user_ids).prefetch_related(
            "departments"
        )
    }
    members = [
        _member_payload(m, dps.get(m.user_id), request.user.id) for m in memberships
    ]

    return JsonResponse(
        {
            "organization": {
                "id": org.id,
                "name": org.name,
                "primary_color": org.primary_color,
            },
            "members": members,
            "available_groups": list(get_access_groups_qs().values("id", "name")),
            "departments": list(
                Department.objects.filter(organization=org).values("id", "name")
            ),
            "chatbot_models": [
                {"id": m.id, "display_name": m.display_name, "identifier": m.identifier}
                for m in ChatbotModel.objects.filter(is_active=True)
            ],
            "settings": {
                "powerbi_tools_enabled": getattr(org, "powerbi_tools_enabled", True),
                "powerbi_live_tools_enabled": getattr(org, "powerbi_live_tools_enabled", False),
                "dbt_tools_enabled": getattr(org, "dbt_tools_enabled", False),
                "bigquery_tools_enabled": getattr(org, "bigquery_tools_enabled", False),
                "bigquery_live_tools_enabled": getattr(org, "bigquery_live_tools_enabled", False),
                "debug_responses_enabled": org.debug_responses_enabled,
                "show_deleted_items": org.show_deleted_items,
                "chatbot_model_id": org.chatbot_model_id,
                "assistant_powerbi_workspace_ids": org.assistant_powerbi_workspace_ids or [],
                "assistant_bigquery_dataset_ids": org.assistant_bigquery_dataset_ids or [],
                "chat_timeout_seconds": org.chat_timeout_seconds,
            },
        }
    )


@require_GET
def org_assistant_scope_view(request):
    """Available PowerBI workspaces + BigQuery datasets for the AI Assistant
    context-scope selectors. Admin-only. Each is ``[{"id","name"}]``; BigQuery
    is fetched live so it may be empty/slow when the client is misconfigured."""
    org, err = _admin_org(request)
    if err:
        return err

    from .tools.assistant import PROVIDERS

    try:
        powerbi = PROVIDERS["powerbi"].scope_options(org)
    except Exception:
        powerbi = []
    try:
        bigquery = PROVIDERS["bigquery"].scope_options(org)
    except Exception:
        bigquery = []
    return JsonResponse({"powerbi": powerbi, "bigquery": bigquery})


@require_POST
def org_members_save_view(request):
    """Create or edit a member. Ported from the classic add-member wizard's
    submit branch (since removed): validate, upsert the CustomUser +
    OrganizationMembership, replace
    the user's page-access groups, and upsert the DataPerson profile.

    Body: {user_id?, email, password?, name, slack_handle, is_owner,
           is_steward, is_other, department_ids[], group_ids[]}.
    ``user_id`` switches to edit mode (email locked to identity, password
    optional — blank leaves it unchanged)."""
    org, err = _admin_org(request)
    if err:
        return err

    from .models import CustomUser, DataPerson, Department

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    name = (data.get("name") or "").strip()
    slack_handle = (data.get("slack_handle") or "").strip()
    is_owner = bool(data.get("is_owner"))
    is_steward = bool(data.get("is_steward"))
    is_other = bool(data.get("is_other"))
    department_ids = [int(x) for x in (data.get("department_ids") or []) if str(x).isdigit()]
    group_ids = [int(x) for x in (data.get("group_ids") or []) if str(x).isdigit()]

    edit_user = None
    if data.get("user_id"):
        edit_user = CustomUser.objects.filter(
            id=data["user_id"], memberships__organization=org
        ).first()
    is_edit = edit_user is not None

    # Validation mirrors the wizard's step-3 submit guard.
    errors = []
    if not is_edit and (not email or not password):
        errors.append("Email and password are required.")
    if not name:
        errors.append("Data person name is required.")
    if not (is_owner or is_steward or is_other):
        errors.append("Select at least one role (Owner, Steward, or Other).")
    if errors:
        return JsonResponse({"error": " ".join(errors)}, status=400)

    available_groups = get_access_groups_qs()
    departments = Department.objects.filter(organization=org)

    if is_edit:
        target_user = edit_user
    else:
        target_user, _ = CustomUser.objects.get_or_create(
            email=email, defaults={"username": email.split("@")[0]}
        )
    # Password: required on create, optional on edit (blank = keep existing).
    if password:
        target_user.set_password(password)
        target_user.save()

    membership, _ = OrganizationMembership.objects.get_or_create(
        user=target_user, organization=org
    )
    # Org-admin is a per-membership flag (not a group). Never let an admin strip
    # their OWN admin access here — that's a lockout guard; changeable for others.
    if "is_admin" in data and target_user.id != request.user.id:
        membership.is_admin = bool(data.get("is_admin"))
        membership.save(update_fields=["is_admin"])

    # Replace page-access groups with the picked ones; preserve any non-access
    # groups the user already belongs to. Empty selection = no page access.
    other_groups = list(target_user.groups.exclude(name__in=ASSIGNABLE_GROUPS))
    chosen = list(available_groups.filter(id__in=group_ids)) if group_ids else []
    target_user.groups.set(chosen + other_groups)

    dp, _ = DataPerson.objects.update_or_create(
        user=target_user,
        defaults={
            "name": name,
            "organization": org,
            "is_owner": is_owner,
            "is_steward": is_steward,
            "is_other": is_other,
            "slack_handle": slack_handle or None,
        },
    )
    dp.departments.set(departments.filter(id__in=department_ids))

    return JsonResponse({"status": "saved", "user_id": target_user.id})


@require_POST
def org_members_remove_view(request):
    """Remove a member from the org. Mirrors the classic ``remove_member``
    action, including the guard against removing yourself."""
    org, err = _admin_org(request)
    if err:
        return err

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    user_id = data.get("user_id")
    if user_id is None:
        return JsonResponse({"error": "user_id is required."}, status=400)
    if str(user_id) == str(request.user.id):
        return JsonResponse({"error": "You cannot remove yourself."}, status=400)

    deleted, _ = OrganizationMembership.objects.filter(
        user_id=user_id, organization=org
    ).delete()
    if not deleted:
        return JsonResponse({"error": "Member not found."}, status=404)
    return JsonResponse({"status": "removed"})


def _q_short_result(value, limit=200, keep_newlines=False):
    """Best-effort string preview of a Django-Q task result (may be any pickled
    object, and unpickling can raise if the class isn't importable here).

    ``keep_newlines`` preserves line breaks for the full detail view; the
    default collapses them to a single line for the table cell."""
    try:
        text = str(value)
    except Exception:
        return None
    if text is None:
        return None
    text = text.strip() if keep_newlines else text.replace("\n", " ").strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


@require_GET
def org_queues_view(request):
    """Django-Q queue snapshot for the Org Settings → Queues tab.

    Returns the live cluster (worker) status, what is waiting in the broker
    (OrmQ), the most recent finished tasks (success/failure), and the scheduled
    jobs — plus rollup counts. Read-only; admin-gated like the rest of /org/."""
    org, err = _admin_org(request)
    if err:
        return err

    from datetime import timedelta

    from django.utils import timezone
    from django_q.models import OrmQ, Schedule, Task

    now = timezone.now()
    day_ago = now - timedelta(hours=24)

    # Live clusters (workers). Stats live in the shared cache/broker; if the
    # worker is down or stats are unreadable, degrade to "no clusters online".
    clusters = []
    try:
        from django_q.status import Stat

        for stat in Stat.get_all():
            try:
                uptime = stat.uptime()
            except Exception:
                uptime = None
            clusters.append(
                {
                    "cluster_id": str(getattr(stat, "cluster_id", "") or ""),
                    "host": getattr(stat, "host", "") or "",
                    "status": getattr(stat, "status", "") or "",
                    "workers": len(getattr(stat, "workers", []) or []),
                    "task_q_size": getattr(stat, "task_q_size", 0) or 0,
                    "uptime_seconds": uptime,
                }
            )
    except Exception:
        clusters = []

    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    # Waiting in the broker (queued, not yet picked up / finished). A row whose
    # lock is in the future has been reserved by a worker and is running; one
    # whose lock is now/past is still waiting to be picked up.
    queued = []
    for q in OrmQ.objects.all().order_by("id")[:100]:
        running = bool(q.lock and q.lock > now)
        queued.append(
            {
                "id": q.id,
                "key": q.key,
                "task_id": _safe(q.task_id),
                "name": _safe(q.name),
                "func": _safe(q.func),
                "locked": q.lock.isoformat() if q.lock else None,
                "state": "running" if running else "waiting",
            }
        )

    # Most recent finished tasks.
    recent = []
    for t in Task.objects.all().order_by("-stopped")[:25]:
        try:
            duration = t.time_taken()
        except Exception:
            duration = None
        recent.append(
            {
                "id": t.id,
                "name": t.name,
                "func": t.func,
                "group": t.group,
                "success": t.success,
                "started": t.started.isoformat() if t.started else None,
                "stopped": t.stopped.isoformat() if t.stopped else None,
                "duration_seconds": duration,
                "attempt_count": t.attempt_count,
                "short_result": _q_short_result(t.result),
                # Full (newline-preserving) result for the row detail popup.
                "result": _q_short_result(t.result, limit=8000, keep_newlines=True),
            }
        )

    # Scheduled jobs.
    type_label = dict(Schedule.TYPE)
    schedules = []
    last_by_task = {}
    sched_task_ids = [s.task for s in Schedule.objects.exclude(task__isnull=True)]
    if sched_task_ids:
        last_by_task = dict(
            Task.objects.filter(id__in=sched_task_ids).values_list("id", "success")
        )
    for s in Schedule.objects.all().order_by("next_run")[:100]:
        schedules.append(
            {
                "id": s.id,
                "name": s.name,
                "func": s.func,
                "schedule_type": type_label.get(s.schedule_type, s.schedule_type),
                "cron": s.cron,
                "minutes": s.minutes,
                "repeats": s.repeats,
                "next_run": s.next_run.isoformat() if s.next_run else None,
                "last_success": last_by_task.get(s.task) if s.task else None,
            }
        )

    counts = {
        "queued": OrmQ.objects.count(),
        "scheduled": Schedule.objects.count(),
        "success_total": Task.objects.filter(success=True).count(),
        "failed_total": Task.objects.filter(success=False).count(),
        "success_24h": Task.objects.filter(success=True, stopped__gte=day_ago).count(),
        "failed_24h": Task.objects.filter(success=False, stopped__gte=day_ago).count(),
    }

    return JsonResponse(
        {
            "online": bool(clusters),
            "clusters": clusters,
            "counts": counts,
            "queued": queued,
            "recent": recent,
            "schedules": schedules,
        }
    )


@require_POST
def org_queue_task_kill_view(request, ormq_id):
    """Terminate a Django-Q task from the broker (OrmQ) by its broker-row id.

    - Waiting task → the broker row is removed, so it never runs.
    - Running task → we signal cooperative cancellation (a known source /
      destination / workflow task stops at its next checkpoint and is marked
      failed) and remove the broker row so a retry can't re-run it.

    Django-Q has no generic ``SIGKILL`` for an arbitrary running function, so a
    task type without checkpoints can only be dropped from the queue (no retry);
    its current in-flight step still finishes on the worker."""
    org, err = _admin_org(request)
    if err:
        return err

    from django_q.models import OrmQ
    from django_q.utils import get_func_repr

    q = OrmQ.objects.filter(id=ormq_id).first()
    if not q:
        return JsonResponse(
            {"error": "Task not found in queue — it may have already started or finished."},
            status=404,
        )

    # Best-effort cooperative-cancel signal for the task types we know how to
    # stop. Derive the target from the (unpickled) payload before deleting it.
    signalled = None
    try:
        from .integration_tasks import (
            request_destination_cancel,
            request_source_cancel,
            request_workflow_cancel,
        )

        task = q.task  # cached dict; {"func": ..., "args": (...), ...}
        func_repr = get_func_repr(task.get("func")) or ""
        args = task.get("args") or ()
        target = args[0] if args else None
        if target is not None:
            if func_repr.endswith("run_source_task"):
                request_source_cancel(target)
                signalled = "source"
            elif func_repr.endswith("run_destination_task"):
                request_destination_cancel(target)
                signalled = "destination"
            elif func_repr.endswith("run_workflow_task"):
                request_workflow_cancel(target)
                signalled = "workflow"
    except Exception:
        signalled = None

    q.delete()
    return JsonResponse({"status": "killed", "signalled": signalled})


@require_POST
def org_settings_save_view(request):
    """Update the org's chatbot feature-flags + display settings. Port of the
    classic ``update_bot_settings`` / ``update_display_settings`` actions."""
    org, err = _admin_org(request)
    if err:
        return err

    from .models import ChatbotModel

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    # Catalog tiers are on by default, so when their key is absent fall back to
    # the current value — an older client that doesn't send them can't silently
    # turn them off. Live tiers and the display flags default to off when absent.
    org.powerbi_tools_enabled = bool(
        data.get("powerbi_tools_enabled", org.powerbi_tools_enabled)
    )
    org.powerbi_live_tools_enabled = bool(data.get("powerbi_live_tools_enabled"))
    org.dbt_tools_enabled = bool(
        data.get("dbt_tools_enabled", org.dbt_tools_enabled)
    )
    org.bigquery_tools_enabled = bool(
        data.get("bigquery_tools_enabled", org.bigquery_tools_enabled)
    )
    org.bigquery_live_tools_enabled = bool(data.get("bigquery_live_tools_enabled"))
    org.debug_responses_enabled = bool(data.get("debug_responses_enabled"))
    org.show_deleted_items = bool(data.get("show_deleted_items"))
    update_fields = [
        "powerbi_tools_enabled",
        "powerbi_live_tools_enabled",
        "dbt_tools_enabled",
        "bigquery_tools_enabled",
        "bigquery_live_tools_enabled",
        "debug_responses_enabled",
        "show_deleted_items",
    ]

    if "chat_timeout_seconds" in data:
        try:
            timeout = int(data.get("chat_timeout_seconds"))
        except (TypeError, ValueError):
            timeout = 180
        # Clamp to a sane range so a bad value can't disable the timeout or
        # pin the worker for too long.
        org.chat_timeout_seconds = max(30, min(timeout, 600))
        update_fields.append("chat_timeout_seconds")

    # Assistant context scope — which PowerBI workspaces / BigQuery datasets
    # feed the front-loaded catalog context.
    if "assistant_powerbi_workspace_ids" in data:
        ids = data.get("assistant_powerbi_workspace_ids") or []
        org.assistant_powerbi_workspace_ids = [str(x) for x in ids if x]
        update_fields.append("assistant_powerbi_workspace_ids")
    if "assistant_bigquery_dataset_ids" in data:
        ids = data.get("assistant_bigquery_dataset_ids") or []
        org.assistant_bigquery_dataset_ids = [str(x) for x in ids if x]
        update_fields.append("assistant_bigquery_dataset_ids")

    if "chatbot_model_id" in data:
        model_id = data.get("chatbot_model_id")
        if model_id:
            try:
                org.chatbot_model = ChatbotModel.objects.get(
                    id=int(model_id), is_active=True
                )
                update_fields.append("chatbot_model")
            except (ChatbotModel.DoesNotExist, ValueError, TypeError):
                pass
        else:
            org.chatbot_model = None
            update_fields.append("chatbot_model")

    org.save(update_fields=update_fields)
    return JsonResponse({"status": "saved"})


# ---------------------------------------------------------------------------
# Per-user default workspaces (ported from the classic User Settings page).
#
# A user can pick a default PowerBI workspace per active source; the chatbot
# uses these (resolve_default_workspaces_for_org) to scope queries. GET returns
# the picker data, POST saves the {source_id: workspace_id} map. Available to
# any authenticated org member (each user manages their OWN defaults).
# ---------------------------------------------------------------------------

def _workspace_sources_payload(user, org):
    """[{id, name, source_type, workspaces:[{id,name}], selected_id, auto_only}]
    for every active PowerBI source in ``org`` that has workspaces."""
    from .models import IntegrationSource
    from .services.workspaces import get_workspaces_for_source

    defaults = user.default_workspaces or {}
    out = []
    sources = IntegrationSource.objects.filter(
        organization=org, is_active=True, source_type="powerbi_fabric"
    ).order_by("name")
    for src in sources:
        workspaces = get_workspaces_for_source(src)
        if not workspaces:
            continue
        out.append({
            "id": src.id,
            "name": src.name,
            "source_type": src.source_type,
            "workspaces": workspaces,
            "selected_id": defaults.get(str(src.id), ""),
            "auto_only": len(workspaces) == 1,
        })
    return out


@require_http_methods(["GET", "POST"])
def me_workspaces_view(request):
    """GET -> the per-source workspace picker; POST {defaults:{source_id:ws_id}}
    saves CustomUser.default_workspaces (blank/absent clears a source)."""
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)

    mem = (
        OrganizationMembership.objects.filter(user=user)
        .select_related("organization")
        .first()
    )
    org = mem.organization if mem else (
        Organization.objects.first() if user.is_superuser else None
    )
    if org is None:
        return JsonResponse({"sources": []})

    if request.method == "POST":
        from .models import IntegrationSource

        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid JSON body."}, status=400)

        provided = data.get("defaults") or {}
        defaults = dict(user.default_workspaces or {})
        # Mirror the classic page: iterate the org's active PowerBI sources and
        # set/clear each from the provided map.
        for src in IntegrationSource.objects.filter(
            organization=org, is_active=True, source_type="powerbi_fabric"
        ):
            value = str(provided.get(str(src.id), "") or "").strip()
            if value:
                defaults[str(src.id)] = value
            else:
                defaults.pop(str(src.id), None)
        user.default_workspaces = defaults
        user.save(update_fields=["default_workspaces"])

    return JsonResponse({"sources": _workspace_sources_payload(user, org)})
