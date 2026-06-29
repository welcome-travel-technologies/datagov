"use client";

import { useEffect, useRef, useState } from "react";
import { Send, Plus, Sparkles, MessageSquare, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "model";
  content: string;
}

interface SessionRef {
  id: number;
  title: string;
  updated_at: string;
}

const POLL_MS = 4000;
// Give up on a dead poll after this many consecutive transient failures and
// fall back to reloading whatever the backend already saved (~20s).
const MAX_POLL_FAILURES = 5;
// The in-flight task is mirrored to sessionStorage so a tab/window switch, an
// SPA navigation away and back, or a reload can resume polling instead of
// stranding the user on a question with no answer and no spinner. Scoped to the
// tab (sessionStorage) on purpose — it should not leak into other tabs.
const PENDING_KEY = "datagov:chat:pending";
const PENDING_TTL_MS = 15 * 60 * 1000;

interface PendingTask {
  taskId: string;
  sessionId: number;
}

function persistPending(p: PendingTask) {
  try {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ ...p, ts: Date.now() }));
  } catch {
    /* storage unavailable (private mode/quota): polling still works this mount */
  }
}

function readPending(): PendingTask | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as Partial<PendingTask> & { ts?: number };
    if (!p.taskId || !p.sessionId) return null;
    if (Date.now() - (p.ts ?? 0) > PENDING_TTL_MS) {
      clearPending();
      return null;
    }
    return { taskId: p.taskId, sessionId: p.sessionId };
  } catch {
    return null;
  }
}

function clearPending() {
  try {
    sessionStorage.removeItem(PENDING_KEY);
  } catch {
    /* ignore */
  }
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRef[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [activeTask, setActiveTask] = useState<PendingTask | null>(null);
  const sessionRef = useRef<number | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (messages.length === 0 && !status) return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, status]);

  async function loadSessions() {
    try {
      setSessions(await api.chat.sessions());
    } catch {
      /* non-fatal: panel just stays empty */
    }
  }

  useEffect(() => {
    loadSessions();
    // Resume an answer that was already in flight before this mount — a tab or
    // OS-window switch, navigating away and back, or a reload. The backend
    // persists the user message and the answer regardless of whether the
    // browser is watching, so we restore the conversation and re-attach the
    // poller instead of showing an empty chat.
    const pending = readPending();
    if (pending) {
      sessionRef.current = pending.sessionId;
      setActiveId(pending.sessionId);
      setBusy(true);
      setStatus("Analyzing metadata…");
      api.chat
        .messages(pending.sessionId)
        .then((msgs) =>
          setMessages(msgs.map((m) => ({ role: m.role === "user" ? "user" : "model", content: m.content }))),
        )
        .catch(() => {
          /* leave chat as-is; the poller reloads messages on completion */
        });
      setActiveTask(pending);
    }
  }, []);

  // Drives the in-flight task to completion. This lives in an effect (not inside
  // send) so it survives re-renders and resumes when activeTask is restored on
  // mount. A single failed poll no longer kills the exchange, and the spinner
  // can never be silently lost: every exit path reloads messages and clears
  // busy/status.
  useEffect(() => {
    if (!activeTask) return;
    const { taskId, sessionId } = activeTask;
    let stopped = false;
    let polling = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function finish() {
      try {
        const msgs = await api.chat.messages(sessionId);
        if (!stopped) {
          setMessages(msgs.map((m) => ({ role: m.role === "user" ? "user" : "model", content: m.content })));
        }
      } catch {
        /* keep whatever is already on screen */
      }
      clearPending();
      setActiveTask(null);
      setBusy(false);
      setStatus(null);
      loadSessions();
    }

    async function tick() {
      if (stopped || polling) return;
      polling = true;
      let terminal = false;
      try {
        const st = await api.chat.taskStatus(taskId);
        if (stopped) return;
        failures = 0;
        if (st.current_status) setStatus(st.current_status);
        if (st.status === "completed" || st.status === "failed") {
          terminal = true;
          await finish();
        }
      } catch (err) {
        // 404 means the task→session mapping expired (>10min); the answer is
        // already persisted, so reload it rather than retrying forever.
        if (err instanceof ApiError && err.status === 404) {
          terminal = true;
          await finish();
        } else if (++failures >= MAX_POLL_FAILURES) {
          terminal = true;
          await finish();
        }
      } finally {
        polling = false;
      }
      if (!stopped && !terminal) timer = setTimeout(tick, POLL_MS);
    }

    // Background tabs throttle timers, so when the tab/window regains focus we
    // poll immediately instead of waiting out a stretched-to-a-minute timeout.
    function onVisible() {
      if (document.visibilityState === "visible" && !stopped && !polling) {
        if (timer) clearTimeout(timer);
        tick();
      }
    }
    document.addEventListener("visibilitychange", onVisible);

    timer = setTimeout(tick, POLL_MS);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [activeTask]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setBusy(true);
    setStatus("Analyzing metadata…");
    try {
      const res = await api.chat.send(text, sessionRef.current);
      if (res.error) throw new Error(res.error);
      const sessionId = res.session_id ?? sessionRef.current;
      if (res.session_id && !sessionRef.current) {
        sessionRef.current = res.session_id;
        setActiveId(res.session_id);
      }
      if (res.task_id && sessionId) {
        // Hand off to the polling effect, which owns busy/status from here and
        // persists the task so it can resume after a tab/window switch.
        persistPending({ taskId: res.task_id, sessionId });
        setActiveTask({ taskId: res.task_id, sessionId });
      } else {
        setBusy(false);
        setStatus(null);
        loadSessions();
      }
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: "model", content: "Sorry, I encountered an error: " + (err instanceof Error ? err.message : "unknown") },
      ]);
      setBusy(false);
      setStatus(null);
    }
  }

  async function openSession(id: number) {
    if (busy || id === activeId) return;
    sessionRef.current = id;
    setActiveId(id);
    setStatus(null);
    try {
      const msgs = await api.chat.messages(id);
      setMessages(msgs.map((m) => ({ role: m.role === "user" ? "user" : "model", content: m.content })));
    } catch {
      setMessages([]);
    }
  }

  async function removeSession(id: number, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api.chat.delete(id);
    } catch {
      return;
    }
    if (sessionRef.current === id) reset();
    setSessions((s) => s.filter((x) => x.id !== id));
  }

  function reset() {
    clearPending();
    setActiveTask(null);
    sessionRef.current = null;
    setActiveId(null);
    setMessages([]);
    setStatus(null);
    setBusy(false);
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="AI Assistant"
        description="Ask about measures, columns, reports, lineage and dependencies."
        actions={
          // Fallback for screens below `lg`, where the History panel (and the
          // "New chat" button it now hosts) is hidden.
          <Button variant="outline" size="sm" onClick={reset} className="lg:hidden">
            <Plus className="h-4 w-4" /> New chat
          </Button>
        }
      />

      <div className="flex min-h-0 flex-1 gap-4">
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line bg-card">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
            {messages.length === 0 && (
              <div className="flex items-start gap-2.5">
                <Avatar />
                <Bubble role="model">
                  Hello! I&apos;m your Data Governance Assistant. I can help you search for measures, columns,
                  reports, or dependencies.
                </Bubble>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={cn("flex items-start gap-2.5", m.role === "user" && "flex-row-reverse")}>
                {m.role === "model" ? <Avatar /> : null}
                <Bubble role={m.role}>
                  {m.role === "model" ? <Markdown>{m.content}</Markdown> : m.content}
                </Bubble>
              </div>
            ))}
            {status && (
              <div className="flex items-start gap-2.5">
                <Avatar />
                <Bubble role="model">
                  <span className="inline-flex items-center gap-2 text-muted-foreground">
                    <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand" />
                    {status}
                  </span>
                </Bubble>
              </div>
            )}
            <div ref={endRef} />
          </div>

          <form onSubmit={send} className="flex items-center gap-2 border-t border-line p-3">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={busy}
              placeholder="Ask about your DataGov"
              className="h-10 flex-1 rounded-lg border border-input bg-panel px-3 text-[13px] outline-none focus:ring-1 focus:ring-ring disabled:opacity-60"
            />
            <Button type="submit" variant="brand" size="icon" disabled={busy || !input.trim()} aria-label="Send">
              <Send className="h-4 w-4" />
            </Button>
          </form>
        </div>

        <aside className="hidden w-72 shrink-0 flex-col overflow-hidden rounded-lg border border-line bg-card lg:flex">
          <div className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
            <span className="text-[13px] font-semibold">History</span>
            <Button variant="outline" size="sm" className="h-7 px-2.5" onClick={reset}>
              <Plus className="h-4 w-4" /> New chat
            </Button>
          </div>
          <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-2">
            {sessions.length === 0 && (
              <p className="px-2 py-3 text-[13px] text-muted-foreground">No previous chats yet.</p>
            )}
            {sessions.map((s) => (
              <button
                key={s.id}
                onClick={() => openSession(s.id)}
                className={cn(
                  "group relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 pr-8 text-left text-[13px] transition-colors",
                  s.id === activeId ? "bg-foreground/[0.06] text-foreground" : "text-muted-foreground hover:bg-foreground/[0.04]",
                )}
              >
                <MessageSquare className="h-4 w-4 shrink-0 opacity-70" />
                <span className="flex-1 truncate">{s.title || "Untitled chat"}</span>
                {/* Absolutely positioned + opacity-toggled so revealing it on hover
                    never changes the row's size (a display toggle made the row grow,
                    which caused a hover/un-hover flicker loop at the cursor edge). */}
                <span
                  role="button"
                  tabIndex={-1}
                  aria-label="Delete chat"
                  onClick={(e) => removeSession(s.id, e)}
                  className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:pointer-events-auto group-hover:opacity-100"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </span>
              </button>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function Avatar() {
  return (
    <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-brand text-white">
      <Sparkles className="h-3.5 w-3.5" />
    </div>
  );
}

function Bubble({ role, children }: { role: "user" | "model"; children: React.ReactNode }) {
  return (
    <div
      className={cn(
        "max-w-[80%] rounded-2xl px-3.5 py-2.5 text-[13px] leading-relaxed shadow-card",
        role === "user"
          // pre-wrap only on plain-text user messages, to keep their typed line
          // breaks. It must NOT wrap the model bubble: white-space inherits, so it
          // would turn react-markdown's cosmetic inter-block "\n" nodes into real
          // blank lines (huge gaps between list items). Markdown handles its own
          // intra-paragraph pre-wrap.
          ? "whitespace-pre-wrap rounded-tr-sm bg-brand text-white"
          : "rounded-tl-sm border border-line bg-panel text-foreground/90",
      )}
    >
      {children}
    </div>
  );
}
