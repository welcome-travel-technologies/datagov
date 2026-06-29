"""
Interactive CLI chat REPL — exercises the assistant from a terminal.

Drives the full pydantic-ai agent (``catalog.tools.get_agent``) exactly
the way the web view does: front-loaded catalog context plus a small set
of tools (PowerBI schema bundle + run-DAX, the dbt model-schema tool, and
the BigQuery query tool). Use it to confirm the agent answers in one or
two tool calls without looping. Conversation history is kept in memory
across turns within one REPL session (the agent is stateful between
``run_sync`` calls via ``message_history``).

Usage
-----

::

    python manage.py chat_repl                       # org id 1
    python manage.py chat_repl --org-id 2
    python manage.py chat_repl --once --seed "..."   # single-shot (no loop)
    python manage.py chat_repl --force-powerbi       # bypass org enable flag

REPL commands
-------------

::

    :quit / :q / :exit       leave the REPL
    :reset                   clear conversation history
    :help                    print this list
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from catalog.models import Organization
from catalog.views import (
    _get_chatbot_model_for_org,
    _get_dbt_enabled_for_org,
)


_DEFAULT_SEED = (
    "compare transfers booked vs transfers operated per channel per "
    "week for april 2026"
)


class Command(BaseCommand):
    help = (
        "Interactive REPL for the Data Governance assistant — drives the "
        "full pydantic-ai agent end-to-end."
    )

    # -----------------------------------------------------------------
    # CLI args
    # -----------------------------------------------------------------

    def add_arguments(self, parser):
        parser.add_argument("--org-id", type=int, default=1)
        parser.add_argument(
            "--seed", type=str, default=_DEFAULT_SEED,
            help="First user message to send before handing control to the REPL. "
                 "Pass --seed \"\" to skip the seed.",
        )
        parser.add_argument(
            "--once", action="store_true",
            help="Send the seed, print the reply, and exit (no REPL loop).",
        )
        parser.add_argument(
            "--surface", choices=("web", "slack"), default="web",
            help="Rendering surface passed to the agent (affects only minor "
                 "formatting hints).",
        )
        parser.add_argument(
            "--force-powerbi", action="store_true",
            help="Build a live PowerBI client even when "
                 "org.powerbi_live_tools_enabled is False. Useful for local "
                 "testing without flipping the org-level flag in admin. Same "
                 "applies to --force-bigquery / --force-dbt for symmetry.",
        )
        parser.add_argument("--force-bigquery", action="store_true")
        parser.add_argument("--force-dbt", action="store_true")

    # -----------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------

    def handle(self, *args, **opts):
        org = Organization.objects.filter(id=opts["org_id"]).first()
        if not org:
            self.stderr.write(self.style.ERROR(
                f"Organization id={opts['org_id']} not found."
            ))
            return

        # Per-org integration handles. ``--force-*`` bypasses the org-level
        # enabled flag for local testing.
        powerbi_client = self._resolve_powerbi_client(
            org, force=opts.get("force_powerbi", False),
        )
        bigquery_client = self._resolve_bigquery_client(
            org, force=opts.get("force_bigquery", False),
        )
        dbt_enabled = (
            opts.get("force_dbt", False) or _get_dbt_enabled_for_org(org)
        )
        model = _get_chatbot_model_for_org(org)
        if not model:
            self.stderr.write(self.style.ERROR(
                "No chatbot model configured. Set a ChatbotModel via "
                "Django admin and assign it to the org."
            ))
            return

        from catalog.services.workspaces import (
            resolve_default_workspaces_for_org,
        )
        workspace_scope = (
            resolve_default_workspaces_for_org(None, org)
            if powerbi_client is not None else None
        )

        runner = _Runner(
            cmd=self,
            powerbi_client=powerbi_client,
            bigquery_client=bigquery_client,
            dbt_enabled=dbt_enabled,
            org=org,
            model=model,
            workspace_scope=workspace_scope,
            surface=opts["surface"],
        )

        self.stdout.write(self.style.SUCCESS(
            f"\nChat REPL ready (org={org.name}, model={model})."
        ))
        self.stdout.write("Type :help for commands.\n")

        seed = (opts.get("seed") or "").strip()
        if seed:
            runner.send(seed)

        if opts["once"]:
            return

        while True:
            try:
                user_input = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.stdout.write("\nbye.")
                return

            if not user_input:
                continue
            if user_input in (":quit", ":q", ":exit"):
                self.stdout.write("bye.")
                return
            if user_input == ":help":
                self.stdout.write(_HELP_TEXT)
                continue
            if user_input == ":reset":
                runner.reset()
                self.stdout.write(self.style.HTTP_INFO("(reset)"))
                continue

            runner.send(user_input)

    # -----------------------------------------------------------------
    # Client resolvers — honor --force-* to bypass org-level gates
    # -----------------------------------------------------------------

    def _resolve_powerbi_client(self, org, *, force: bool):
        """Return a live PowerBI client. With ``force=True`` we bypass the
        ``org.powerbi_live_tools_enabled`` gate (the production code path
        respects it; for local REPL testing we skip it on demand)."""
        if force:
            try:
                from catalog.powerbi_client import build_powerbi_client_for_org
                client = build_powerbi_client_for_org(org)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f"--force-powerbi: build_powerbi_client_for_org raised "
                    f"{exc.__class__.__name__}: {exc}"
                ))
                return None
            if client is None:
                self.stderr.write(self.style.WARNING(
                    "--force-powerbi: builder returned None — likely no "
                    "active IntegrationSource of type 'powerbi_fabric' "
                    f"for org {org.name!r}."
                ))
            else:
                self.stdout.write(self.style.HTTP_INFO(
                    "  [forced powerbi client (org flag bypassed)]"
                ))
            return client

        from catalog.views import _get_powerbi_client_for_org
        return _get_powerbi_client_for_org(org)

    def _resolve_bigquery_client(self, org, *, force: bool):
        """Same as ``_resolve_powerbi_client`` but for BigQuery."""
        if force:
            try:
                from catalog.bigquery_client import build_bigquery_client_for_org
                client = build_bigquery_client_for_org(org)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f"--force-bigquery: build_bigquery_client_for_org raised "
                    f"{exc.__class__.__name__}: {exc}"
                ))
                return None
            if client is None:
                self.stderr.write(self.style.WARNING(
                    "--force-bigquery: builder returned None."
                ))
            else:
                self.stdout.write(self.style.HTTP_INFO(
                    "  [forced bigquery client (org flag bypassed)]"
                ))
            return client

        from catalog.views import _get_bigquery_client_for_org
        return _get_bigquery_client_for_org(org)


# ---------------------------------------------------------------------------
# Runner — drives the full pydantic-ai agent
# ---------------------------------------------------------------------------

class _Runner:
    def __init__(
        self, *, cmd, powerbi_client, bigquery_client, dbt_enabled,
        org, model, workspace_scope, surface,
    ):
        self.cmd = cmd
        from catalog.tools import get_agent
        self.agent = get_agent(
            powerbi_client=powerbi_client,
            bigquery_client=bigquery_client,
            dbt_enabled=dbt_enabled,
            # REPL mirrors prod defaults: PowerBI catalog always on; BigQuery live
            # SQL enabled whenever a BigQuery client was built (force or org).
            powerbi_tools_enabled=True,
            bigquery_live_enabled=bigquery_client is not None,
            before_tool_call=self._print_call,
            model=model,
            workspace_scope=workspace_scope,
            surface=surface,
            org=org,
        )
        self.history: list = []

    def _print_call(self, tool_name, _args=(), kwargs=None):
        kwargs = kwargs or {}
        extra = ""
        if kwargs.get("dax_query"):
            extra = f" dax={(kwargs['dax_query'] or '')[:80]!r}"
        elif kwargs.get("sql"):
            extra = f" sql={(kwargs['sql'] or '')[:80]!r}"
        elif kwargs.get("measure_name_or_id"):
            extra = f" measure={kwargs['measure_name_or_id']!r}"
        elif kwargs.get("query"):
            extra = f" query={kwargs['query']!r}"
        self.cmd.stdout.write(self.cmd.style.WARNING(f"  [tool] {tool_name}{extra}"))

    def send(self, message: str) -> None:
        self.cmd.stdout.write(self.cmd.style.NOTICE(f"\n> {message}\n"))
        t0 = time.monotonic()
        try:
            # Run agent.run_sync in a FRESH thread per turn — pydantic-ai
            # (via httpx/anyio) caches loop-bound state on the calling
            # thread. On the second REPL turn that cached state references
            # an already-closed loop and we get "Event loop is closed".
            # The web view dodges this because every HTTP request is on a
            # fresh worker thread; in the REPL we recreate that isolation.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    self.agent.run_sync, message, message_history=self.history,
                )
                result = fut.result()
        except Exception as exc:
            self.cmd.stderr.write(self.cmd.style.ERROR(
                f"Agent run failed: {exc.__class__.__name__}: {exc}"
            ))
            return
        self.history = result.all_messages()
        elapsed = time.monotonic() - t0
        self.cmd.stdout.write("\n" + (result.output or "(empty response)") + "\n")
        self.cmd.stdout.write(self.cmd.style.HTTP_INFO(f"\n  [{elapsed:.1f}s]"))

    def reset(self) -> None:
        self.history = []


_HELP_TEXT = (
    "Commands:\n"
    "  :quit / :q / :exit       leave the REPL\n"
    "  :reset                   clear conversation history\n"
    "  :help                    show this list\n"
)
