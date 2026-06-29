"""
Management command: test_ai_dax_prompt

End-to-end test of the full AI → DAX → PowerBI chain:
  1. Load the org and active PowerBI integration source
  2. Authenticate a PowerBIClient
  3. Build the Pydantic AI agent (catalog + PowerBI tools)
  4. Send a natural-language prompt
  5. Print each tool status in real-time as the agent fires them
     (same messages the chat bubble shows: "Running DAX query on PowerBI…")
  6. Print the full reasoning chain (all messages) after completion
  7. Print a DAX summary — every DAX query the agent executed
  8. Print the final natural-language response

Usage:
    python manage.py test_ai_dax_prompt
    python manage.py test_ai_dax_prompt --org-id 1 --prompt "What is Total Bookings right now?"
"""
import json

from django.core.management.base import BaseCommand

from catalog.models import IntegrationSource, Organization
from catalog.powerbi_client import PowerBIClient
from catalog.tools import get_agent


_DEFAULT_PROMPT = (
    "What is the current live value of the most used revenue measure in our catalog? "
    "Run the DAX query and tell me the result."
)

# Human-readable status messages — mirrors TOOL_STATUS_MESSAGES in views.py
_TOOL_STATUS = {
    'search_pb_columns':          'Looking up PowerBI columns and schemas…',
    'get_pb_measure_dependencies':'Fetching measure dependencies and DAX…',
    'verify_pb_measure_dimension_link': 'Verifying measure↔dimension relationship path…',
    'preview_pb_dbt_bridge':      'Previewing PowerBI ↔ dbt bridge candidates…',
    'get_dbt_upstream_tree':      'Walking dbt upstream lineage tree…',
    'get_lineage':                'Tracing data lineage…',
    'powerbi_run_dax_query':      'Running DAX query on PowerBI…',
    'powerbi_list_workspaces':    'Listing PowerBI workspaces…',
    'powerbi_get_refresh_history':'Fetching refresh history…',
    'powerbi_list_datasets':      'Listing PowerBI datasets…',
}


class Command(BaseCommand):
    help = "End-to-end test: natural-language prompt → AI agent → DAX → PowerBI response."

    # ------------------------------------------------------------------
    # CLI arguments
    # ------------------------------------------------------------------

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt",
            type=str,
            default=_DEFAULT_PROMPT,
            help="The natural-language prompt to send to the agent.",
        )
        parser.add_argument("--org-id", type=int, default=1)

    # ------------------------------------------------------------------
    # Pretty-print helpers (same style as test_powerbi_dax.py)
    # ------------------------------------------------------------------

    def _ok(self, msg):
        self.stdout.write(self.style.SUCCESS(f"  [OK]   {msg}"))

    def _fail(self, msg):
        self.stderr.write(self.style.ERROR(f"  [FAIL] {msg}"))

    def _info(self, msg):
        self.stdout.write(f"  {msg}")

    def _section(self, title):
        self.stdout.write(f'\n{"=" * 60}')
        self.stdout.write(f"  {title}")
        self.stdout.write("=" * 60)

    def _print_message(self, idx: int, msg):
        """
        Walk a single Pydantic AI message and print its content in a
        human-readable way.  Handles ModelRequest, ModelResponse, and the
        various part types (TextPart, ToolCallPart, ToolReturnPart,
        RetryPromptPart).
        """
        kind = type(msg).__name__
        self.stdout.write(f"\n  ── Message {idx} [{kind}] ──")

        parts = getattr(msg, "parts", [])
        for part in parts:
            part_kind = type(part).__name__

            if part_kind == "TextPart":
                text = getattr(part, "content", "")
                self.stdout.write(f"    [Text]  {text[:500]}")

            elif part_kind == "ToolCallPart":
                tool_name = getattr(part, "tool_name", "?")
                args_raw = getattr(part, "args", None)
                if hasattr(args_raw, "args_dict"):
                    args_display = json.dumps(args_raw.args_dict, indent=6)
                elif isinstance(args_raw, dict):
                    args_display = json.dumps(args_raw, indent=6)
                elif isinstance(args_raw, str):
                    args_display = args_raw
                else:
                    args_display = str(args_raw)
                self.stdout.write(self.style.WARNING(
                    f"    [Tool Call]  {tool_name}"
                ))
                self.stdout.write(f"      args: {args_display}")

            elif part_kind == "ToolReturnPart":
                tool_name = getattr(part, "tool_name", "?")
                content = getattr(part, "content", "")
                preview = str(content)
                if len(preview) > 800:
                    preview = preview[:800] + "\n      … (truncated)"
                self.stdout.write(self.style.SUCCESS(
                    f"    [Tool Result] {tool_name}"
                ))
                self.stdout.write(f"      {preview}")

            elif part_kind == "RetryPromptPart":
                content = getattr(part, "content", "")
                self.stdout.write(self.style.ERROR(
                    f"    [Retry Prompt]  {content}"
                ))

            else:
                self.stdout.write(f"    [{part_kind}]  {str(part)[:300]}")

    def _extract_dax_queries(self, all_messages) -> list[dict]:
        """
        Walk all messages and collect every DAX query the agent passed to
        powerbi_run_dax_query (or its safe_ wrapper).  Returns a list of
        dicts with keys: tool_name, dataset_id, workspace_id, dax_query.
        """
        queries = []
        for msg in all_messages:
            for part in getattr(msg, "parts", []):
                if type(part).__name__ != "ToolCallPart":
                    continue
                tool_name = getattr(part, "tool_name", "")
                if "powerbi_run_dax_query" not in tool_name:
                    continue
                args_raw = getattr(part, "args", None)
                if hasattr(args_raw, "args_dict"):
                    args = args_raw.args_dict
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else {}
                    except Exception:
                        args = {}
                queries.append({
                    "tool_name":   tool_name,
                    "dataset_id":  args.get("dataset_id", "?"),
                    "workspace_id": args.get("workspace_id", ""),
                    "dax_query":   args.get("dax_query", "?"),
                })
        return queries

    # ------------------------------------------------------------------
    # Main handler
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        prompt = options["prompt"]
        org_id = options["org_id"]

        # ── STEP 1 — Setup ─────────────────────────────────────────────
        self._section("STEP 1 — Setup: load org & integration source")
        try:
            org = Organization.objects.get(pk=org_id)
            self._ok(
                f"Org: {org.name}  (powerbi_tools_enabled={org.powerbi_tools_enabled}, "
                f"powerbi_live_tools_enabled="
                f"{getattr(org, 'powerbi_live_tools_enabled', False)})"
            )
        except Organization.DoesNotExist:
            self._fail(f"Organization with pk={org_id} not found.")
            return

        src = IntegrationSource.objects.filter(
            organization=org, source_type="powerbi_fabric", is_active=True
        ).first()
        if not src:
            self._fail("No active PowerBI integration source found for this org.")
            return
        self._ok(f"Source: {src.name}")

        # ── STEP 2 — Auth ──────────────────────────────────────────────
        self._section("STEP 2 — Authenticate PowerBI client")
        try:
            client = PowerBIClient(src.tenant_id, src.client_id, src.client_secret)
            client._ensure_token()
            self._ok("Azure AD token acquired successfully")
        except Exception as exc:
            self._fail(f"Authentication failed: {exc}")
            return

        # ── STEP 3 — Build agent ────────────────────────────────────────
        self._section("STEP 3 — Build Pydantic AI agent (catalog + PowerBI tools)")

        # Real-time callback — fires just before each tool executes.
        # Receives (tool_name, positional_args, keyword_args) from make_safe_tool.
        # Always prints: "⚙  <status message> (key=value, ...)"
        stdout = self.stdout
        style  = self.style

        def _on_tool_call(tool_name: str, _args: tuple, kwargs: dict) -> None:
            status_msg = _TOOL_STATUS.get(tool_name, f'Running {tool_name.replace("_", " ")}…')

            # Build the inline context shown after the status message
            parts = []
            if kwargs.get("query"):
                parts.append(f"query={kwargs['query']!r}")
            if kwargs.get("node_name"):
                parts.append(f"node_name={kwargs['node_name']!r}")
            if kwargs.get("dax_query"):
                dax = kwargs["dax_query"]
                parts.append(f"dax={dax[:80]!r}" if len(dax) > 80 else f"dax={dax!r}")
            if kwargs.get("dataset_id"):
                parts.append(f"dataset_id={kwargs['dataset_id']!r}")
            if kwargs.get("workspace_id"):
                parts.append(f"workspace_id={kwargs['workspace_id']!r}")
            # Fallback: show all kwargs for unknown tools
            if not parts and kwargs:
                parts = [f"{k}={v!r}" for k, v in list(kwargs.items())[:3]]

            suffix = f" ({', '.join(parts)})" if parts else ""
            stdout.write(style.WARNING(f"\n  ⚙  {status_msg}{suffix}"))

        try:
            agent = get_agent(powerbi_client=client, before_tool_call=_on_tool_call)
            tool_names = list(agent._function_toolset.tools.keys())  # noqa: SLF001
            self._ok(f"Agent created. Registered tools ({len(tool_names)}):")
            for tn in tool_names:
                self._info(f"  • {tn}")
        except Exception as exc:
            self._fail(f"Agent creation failed: {exc}")
            return

        # ── STEP 4 — Send prompt ────────────────────────────────────────
        self._section("STEP 4 — Sending prompt to agent")
        self.stdout.write(f'\n  Prompt:\n  "{prompt}"\n')
        self.stdout.write('  (tool status messages will appear below in real-time)\n')

        try:
            result = agent.run_sync(prompt)
        except Exception as exc:
            self._fail(f"Agent run failed: {exc}")
            return

        # ── STEP 5 — Full reasoning chain ───────────────────────────────
        self._section("STEP 5 — Agent reasoning chain (all messages)")
        all_msgs = result.all_messages()
        self._info(f"Total messages in chain: {len(all_msgs)}")
        for idx, msg in enumerate(all_msgs, start=1):
            self._print_message(idx, msg)

        # ── STEP 6 — DAX queries summary ────────────────────────────────
        self._section("STEP 6 — DAX queries executed")
        dax_queries = self._extract_dax_queries(all_msgs)
        if not dax_queries:
            self._info("No DAX queries were executed in this run.")
        else:
            self._ok(f"{len(dax_queries)} DAX query/queries executed:")
            for i, q in enumerate(dax_queries, 1):
                self.stdout.write(f"\n  ── DAX Query #{i} ──")
                self._info(f"Dataset ID  : {q['dataset_id']}")
                if q["workspace_id"]:
                    self._info(f"Workspace ID: {q['workspace_id']}")
                self.stdout.write(self.style.WARNING(
                    f"\n{q['dax_query']}\n"
                ))

        # ── STEP 7 — Final response ─────────────────────────────────────
        self._section("STEP 7 — Final agent response")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(result.output))
        self.stdout.write("")

        self._section("DONE")
        self.stdout.write("")
