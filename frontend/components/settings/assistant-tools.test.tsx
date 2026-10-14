import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AssistantTools } from "./assistant-tools";

vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: 7 } }) }));
let client: QueryClient;
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

it("cannot enable administrator-disabled tools and only saves the changed personal preference", async () => {
  const data = { tools: [
    { id: "documents", label: "Document search", description: "Find evidence", allowed: false, available: true, selected: true, effective: false },
    { id: "dbt", label: "dbt catalog", description: "Read models", allowed: true, available: true, selected: true, effective: true },
  ] };
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><AssistantTools /></QueryClientProvider>);
  expect(await screen.findByRole("switch", { name: "Document search" })).toBeDisabled();
  fireEvent.click(screen.getByRole("switch", { name: "dbt catalog" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/me/assistant-tools/", expect.objectContaining({ method: "PATCH" })));
  const write = fetchMock.mock.calls.find(([, options]) => options?.method === "PATCH");
  expect(JSON.parse(write![1].body)).toEqual({ preferences: { dbt: false } });
});
