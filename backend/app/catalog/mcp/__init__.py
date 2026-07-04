"""MCP (Model Context Protocol) server for the data catalog.

Exposes the SAME tool layer the chat assistant uses (item profilers, usage
analytics, lineage, and — scope-gated — live DAX / BigQuery SQL) to any MCP
client over Streamable HTTP at ``POST /api/mcp/``, authenticated with
``McpApiKey`` bearer tokens.

Design (see docs/mcp-server-plan.md):
  - ``auth.py``     — bearer-key mint/verify + the OAuth-style scope names.
  - ``registry.py`` — per-key tool specs: scopes ∩ org feature-flags decide
                      which tools are listed; input schemas are introspected
                      from the tool functions so they never drift from chat.
  - ``views.py``    — stateless JSON-RPC endpoint (initialize / ping /
                      tools/list / tools/call); tool failures come back as
                      MCP ``isError`` results, never HTTP 500s.
"""
