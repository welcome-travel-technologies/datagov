import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemberDialog } from "./member-dialog";
import type { OrgMember } from "@/lib/api";

const fetchMock = vi.fn<typeof fetch>();
const saved = vi.fn();
const close = vi.fn();
let client: QueryClient;
const member: OrgMember = {
  user_id: 7, email: "alex@example.test", username: "alex", display_name: "Alex", is_admin: false, is_self: false,
  group_ids: [1], department_ids: [], is_owner: true, is_steward: false, is_other: false,
  slack_handle: "@alex", slack_user_id: "U0123456789",
};
function mount(value: OrgMember | null = member) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><MemberDialog open onOpenChange={close} member={value}
    groups={[{ id: 1, name: "Company" }]} departments={[]} onSaved={saved} /></QueryClientProvider>);
}
beforeEach(() => {
  saved.mockReset(); close.mockReset(); fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ status: "success", user_id: 7 }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals(); });

describe("organization member Slack identity", () => {
  it("preserves existing tool access and submits an administrator change with the member", async () => {
    mount({ ...member, assistant_tool_access: { documents: false } });
    fireEvent.click(screen.getByText("AI tool access"));
    expect(screen.getByRole("switch", { name: "Allow Document search" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("switch", { name: "Allow dbt catalog" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).assistant_tool_access).toEqual({ documents: false, dbt: false });
  });
  it("loads the existing mapping and sends an edited stable ID separately from the display handle", async () => {
    mount();
    expect(screen.getByLabelText("Slack member ID")).toHaveValue("U0123456789");
    expect(screen.getByLabelText("Slack handle")).toHaveValue("@alex");
    fireEvent.change(screen.getByLabelText("Slack member ID"), { target: { value: " w9876543210 " } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledWith("/api/org/members/save/", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ user_id: 7, slack_handle: "@alex", slack_user_id: "W9876543210" });
    expect(close).toHaveBeenCalledWith(false);
  });

  it("allows an administrator to explicitly remove the Slack mapping", async () => {
    mount();
    fireEvent.change(screen.getByLabelText("Slack member ID"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ slack_user_id: "", slack_handle: "@alex" });
  });

  it.each(["@alex", "U1"])("rejects an invalid member ID %s without sending a write", (value) => {
    mount();
    fireEvent.change(screen.getByLabelText("Slack member ID"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByText("Use a Slack member ID starting with U or W, or leave it blank.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("starts new members with an empty mapping and displays backend conflicts without closing", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: "This Slack member is already linked to another person." }), { status: 400, headers: { "Content-Type": "application/json" } }));
    mount(null);
    expect(screen.getByLabelText("Slack member ID")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "fixture-password" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "New member" } });
    fireEvent.change(screen.getByLabelText("Slack member ID"), { target: { value: "U0123456789" } });
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByText("This Slack member is already linked to another person.")).toBeInTheDocument();
    expect(saved).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });
});
