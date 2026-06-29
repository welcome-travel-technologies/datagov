"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { UserPlus, Save, ShieldCheck } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { api, getApiErrorMessage, type DepartmentRef, type GroupRef, type OrgMember } from "@/lib/api";

interface Draft {
  email: string;
  password: string;
  name: string;
  slack_handle: string;
  is_owner: boolean;
  is_steward: boolean;
  is_other: boolean;
  is_admin: boolean;
  department_ids: number[];
  group_ids: number[];
}

const BLANK: Draft = {
  email: "",
  password: "",
  name: "",
  slack_handle: "",
  is_owner: true,
  is_steward: false,
  is_other: false,
  is_admin: false,
  department_ids: [],
  group_ids: [],
};

function fromMember(m: OrgMember): Draft {
  return {
    email: m.email,
    password: "",
    name: m.display_name === "-" ? "" : m.display_name,
    slack_handle: m.slack_handle,
    is_owner: m.is_owner,
    is_steward: m.is_steward,
    is_other: m.is_other,
    is_admin: m.is_admin,
    department_ids: [...m.department_ids],
    group_ids: [...m.group_ids],
  };
}

/** A pill that toggles membership of `id` in a number[] selection. */
function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-[12.5px] font-medium transition-colors",
        active
          ? "border-brand bg-brand/10 text-brand"
          : "border-line-strong bg-panel text-muted-foreground hover:bg-panel2",
      )}
    >
      {children}
    </button>
  );
}

export function MemberDialog({
  open,
  onOpenChange,
  member,
  groups,
  departments,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  member: OrgMember | null;
  groups: GroupRef[];
  departments: DepartmentRef[];
  onSaved: () => void;
}) {
  const isEdit = member !== null;
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever the dialog opens (or the target member changes).
  useEffect(() => {
    if (open) {
      setDraft(member ? fromMember(member) : BLANK);
      setError(null);
    }
  }, [open, member]);

  const isSelf = !!member?.is_self;

  const saveMut = useMutation({
    mutationFn: () => {
      const body = {
        ...(isEdit ? { user_id: member!.user_id } : { email: draft.email.trim() }),
        ...(draft.password ? { password: draft.password } : {}),
        name: draft.name.trim(),
        slack_handle: draft.slack_handle.trim(),
        is_owner: draft.is_owner,
        is_steward: draft.is_steward,
        is_other: draft.is_other,
        // The server ignores is_admin for the requesting user (self-lockout guard).
        is_admin: draft.is_admin,
        department_ids: draft.department_ids,
        group_ids: draft.group_ids,
      };
      return api.org.saveMember(body);
    },
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not save member.")),
  });

  function validateLocal(): string | null {
    if (!isEdit && (!draft.email.trim() || !draft.password)) return "Email and password are required.";
    if (!draft.name.trim()) return "Name is required.";
    if (!(draft.is_owner || draft.is_steward || draft.is_other))
      return "Select at least one role (Owner, Steward, or Other).";
    return null;
  }

  function submit() {
    const localErr = validateLocal();
    if (localErr) {
      setError(localErr);
      return;
    }
    setError(null);
    saveMut.mutate();
  }

  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const toggleId = (key: "department_ids" | "group_ids", id: number) =>
    setDraft((d) => ({
      ...d,
      [key]: d[key].includes(id) ? d[key].filter((x) => x !== id) : [...d[key], id],
    }));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit member" : "Add member"}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update this member's profile, page access, and governance roles."
              : "Create a login, profile, and page access for a new organization member."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Email">
              {isEdit ? (
                <div className="flex h-9 items-center rounded-md border border-input bg-panel2 px-3 text-[13px] text-muted-foreground">
                  {draft.email}
                </div>
              ) : (
                <Input
                  type="email"
                  value={draft.email}
                  onChange={(e) => set({ email: e.target.value })}
                  placeholder="person@company.com"
                />
              )}
            </Field>
            <Field label={isEdit ? "Password (blank = unchanged)" : "Password"}>
              <Input
                type="password"
                value={draft.password}
                onChange={(e) => set({ password: e.target.value })}
                placeholder={isEdit ? "••••••••" : "Set a password"}
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Display name">
              <Input
                value={draft.name}
                onChange={(e) => set({ name: e.target.value })}
                placeholder="Jane Doe"
              />
            </Field>
            <Field label="Slack handle">
              <Input
                value={draft.slack_handle}
                onChange={(e) => set({ slack_handle: e.target.value })}
                placeholder="@jane"
              />
            </Field>
          </div>

          <Field label="Governance roles">
            <div className="flex flex-wrap gap-2">
              <Chip active={draft.is_owner} onClick={() => set({ is_owner: !draft.is_owner })}>
                Owner
              </Chip>
              <Chip active={draft.is_steward} onClick={() => set({ is_steward: !draft.is_steward })}>
                Steward
              </Chip>
              <Chip active={draft.is_other} onClick={() => set({ is_other: !draft.is_other })}>
                Other
              </Chip>
            </div>
          </Field>

          <Field label="Page access">
            <div className="flex flex-wrap gap-2">
              {groups.map((g) => (
                <Chip
                  key={g.id}
                  active={draft.group_ids.includes(g.id)}
                  onClick={() => toggleId("group_ids", g.id)}
                >
                  {g.name}
                </Chip>
              ))}
              {groups.length === 0 && <span className="text-[12px] text-faint">No access groups defined.</span>}
            </div>
          </Field>

          <div className="flex items-center justify-between gap-4 rounded-lg border border-line bg-panel2/40 px-3 py-2.5">
            <div className="flex items-start gap-2">
              <ShieldCheck className="mt-0.5 h-4 w-4 text-brand" />
              <div>
                <div className="text-[13px] font-medium">Organization admin</div>
                <div className="text-[12px] text-muted-foreground">
                  {isSelf
                    ? "You can't change your own admin access."
                    : "Full access to members, settings, and integrations."}
                </div>
              </div>
            </div>
            <Switch
              checked={draft.is_admin}
              disabled={isSelf}
              onCheckedChange={(v) => set({ is_admin: v })}
            />
          </div>

          {departments.length > 0 && (
            <Field label="Departments">
              <div className="flex flex-wrap gap-2">
                {departments.map((d) => (
                  <Chip
                    key={d.id}
                    active={draft.department_ids.includes(d.id)}
                    onClick={() => toggleId("department_ids", d.id)}
                  >
                    {d.name}
                  </Chip>
                ))}
              </div>
            </Field>
          )}

          {error && <FormError>{error}</FormError>}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="brand" onClick={submit} disabled={saveMut.isPending}>
            {isEdit ? <Save /> : <UserPlus />}
            {saveMut.isPending ? "Saving…" : isEdit ? "Save changes" : "Add member"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
