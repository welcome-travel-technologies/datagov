"use client";

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, Trash2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Field } from "@/components/ui/form-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Spinner, EmptyState } from "@/components/ui/misc";
import { api, getApiErrorMessage, type IntegrationHook } from "@/lib/api";

const QK = ["integrations"];

/** Bot tab — the Slack bot hook (`hook_type === 'slack'`) used by the
 * Governance Assistant. The create endpoint always makes a `slack` bot hook,
 * so when none exists we show the create card. */
export function BotTab({ hooks }: { hooks: IntegrationHook[] }) {
  const botHook = hooks.find((h) => h.hook_type === "slack") ?? null;
  return botHook ? <HookCard hook={botHook} kind="bot" /> : <AddBotCard />;
}

/** Alerts tab — the run/status notification hook. Prefers a dedicated
 * `slack_alerts` hook, falling back to the `slack` bot hook (which is what the
 * original template did: edit the alert channel on the shared hook). When no
 * Slack bot is connected at all, point the user to the Bot tab. */
export function AlertsTab({ hooks }: { hooks: IntegrationHook[] }) {
  const alertsHook = hooks.find((h) => h.hook_type === "slack_alerts") ?? null;
  const botHook = hooks.find((h) => h.hook_type === "slack") ?? null;
  const hook = alertsHook ?? botHook;

  if (!hook) {
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            title="No Slack bot connected"
            hint="Connect a Slack bot in the Bot tab first, then set an alert channel here."
          />
        </CardContent>
      </Card>
    );
  }
  return <HookCard hook={hook} kind="alerts" />;
}

function HookCard({ hook, kind }: { hook: IntegrationHook; kind: "bot" | "alerts" }) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: QK });

  const isBot = kind === "bot";
  const initialChannel = isBot ? hook.slack_channel : hook.slack_alerts_channel || hook.slack_channel;

  const [token, setToken] = useState("");
  const [channel, setChannel] = useState(initialChannel);

  // Re-seed from the latest hook data whenever it changes.
  useEffect(() => {
    setToken("");
    setChannel(initialChannel);
  }, [initialChannel]);

  const connected = hook.is_active && hook.slack_bot_token_set;

  const saveMut = useMutation({
    mutationFn: () => {
      const body: Parameters<typeof api.integrations.saveHook>[0] = {
        id: hook.id,
        is_active: true,
      };
      if (token.trim()) body.slack_bot_token = token.trim();
      if (isBot) body.slack_channel = channel.trim();
      else body.slack_alerts_channel = channel.trim();
      return api.integrations.saveHook(body);
    },
    onSuccess: () => {
      setToken("");
      invalidate();
    },
  });

  const disconnectMut = useMutation({
    mutationFn: () => api.integrations.saveHook({ id: hook.id, disconnect: true }),
    onSuccess: invalidate,
  });

  function onDisconnect() {
    if (!window.confirm("Disconnect Slack?")) return;
    disconnectMut.mutate();
  }

  return (
    <Card>
      <CardContent className="space-y-4 pt-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-[14px] font-semibold">{isBot ? "Slack Bot" : "Slack Alerts"}</h3>
            <p className="mt-0.5 text-[12px] text-muted-foreground">
              {isBot
                ? "The Governance Assistant responds to @mentions in your Slack workspace."
                : "Posts a message when a source or destination run completes, or an item status changes."}
            </p>
          </div>
          <Badge variant={connected ? "success" : "outline"} dot>
            {connected ? "Connected" : "Not connected"}
          </Badge>
        </div>

        <Field label="Bot Token (xoxb-…)">
          <Input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={hook.slack_bot_token_set ? "••• saved — paste to update" : "xoxb-your-bot-token"}
            className="font-mono"
          />
        </Field>

        <Field label={isBot ? "Bot Channel (e.g. #data-bot)" : "Alert Channel (e.g. #data-alerts)"}>
          <Input
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
            placeholder={isBot ? "#data-bot" : "#data-alerts"}
          />
        </Field>

        <p className="text-[12px] text-muted-foreground">
          <span className="font-medium text-foreground">How to get your Bot Token:</span> create an
          app at{" "}
          <a
            href="https://api.slack.com/apps"
            target="_blank"
            rel="noreferrer"
            className="text-brand underline"
          >
            api.slack.com/apps
          </a>
          , add the <code className="rounded bg-panel2 px-1">chat:write</code>
          {isBot ? " (plus app_mentions:read + *:history) " : " "}
          scope, install to your workspace, then copy the Bot User OAuth Token.
        </p>

        {(saveMut.isError || disconnectMut.isError) && (
          <p className="text-[12.5px] text-err">
            {getApiErrorMessage(saveMut.error, getApiErrorMessage(disconnectMut.error, "Could not save Slack settings."))}
          </p>
        )}

        <div className="flex items-center justify-between pt-1">
          {connected ? (
            <button
              onClick={onDisconnect}
              disabled={disconnectMut.isPending}
              className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-err hover:text-err/80 disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" /> Disconnect
            </button>
          ) : (
            <span />
          )}
          <Button variant="brand" size="sm" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            {saveMut.isPending ? <Spinner className="text-white" /> : <Save />} Save
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** Shown when no Slack bot hook exists. Creating a hook (no id) always makes a
 * `slack` bot hook on the backend. */
function AddBotCard() {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [channel, setChannel] = useState("");

  const createMut = useMutation({
    mutationFn: () =>
      api.integrations.saveHook({
        is_active: true,
        slack_bot_token: token.trim(),
        slack_channel: channel.trim(),
      }),
    onSuccess: () => {
      setToken("");
      setChannel("");
      qc.invalidateQueries({ queryKey: QK });
    },
  });

  return (
    <Card>
      <CardContent className="space-y-4 pt-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-[14px] font-semibold">Add Slack bot</h3>
            <p className="mt-0.5 text-[12px] text-muted-foreground">
              Connect a Slack bot to enable the Governance Assistant and run alerts.
            </p>
          </div>
          <Badge variant="outline" dot>
            Not connected
          </Badge>
        </div>

        <Field label="Bot Token (xoxb-…)">
          <Input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="xoxb-your-bot-token"
            className="font-mono"
          />
        </Field>
        <Field label="Bot Channel (e.g. #data-bot)">
          <Input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="#data-bot" />
        </Field>

        <p className="text-[12px] text-muted-foreground">
          <span className="font-medium text-foreground">How to get your Bot Token:</span> create an
          app at{" "}
          <a
            href="https://api.slack.com/apps"
            target="_blank"
            rel="noreferrer"
            className="text-brand underline"
          >
            api.slack.com/apps
          </a>
          , add the <code className="rounded bg-panel2 px-1">chat:write</code> scope, install to your
          workspace, then copy the Bot User OAuth Token.
        </p>

        {createMut.isError && (
          <p className="text-[12.5px] text-err">
            {getApiErrorMessage(createMut.error, "Could not add Slack bot.")}
          </p>
        )}

        <div className="flex justify-end pt-1">
          <Button
            variant="brand"
            size="sm"
            onClick={() => createMut.mutate()}
            disabled={createMut.isPending || !token.trim()}
          >
            {createMut.isPending ? <Spinner className="text-white" /> : <Save />} Add Slack bot
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
