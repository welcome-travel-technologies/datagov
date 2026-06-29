"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { PageHeader } from "@/components/page-header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { WorkspaceDefaults } from "@/components/settings/workspace-defaults";
import { useAuth } from "@/lib/auth";
import { api, ApiError } from "@/lib/api";

export default function UserSettingsPage() {
  const { user, logout } = useAuth();
  const perms = user?.perms;
  const granted = perms ? Object.entries(perms).filter(([, v]) => v) : [];

  return (
    <div>
      <PageHeader title="User Settings" description="Your account and access within the DataGov." />

      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="security">Security</TabsTrigger>
        </TabsList>

        <TabsContent value="profile">
          <div className="grid gap-4 md:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Account</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-[13px]">
                <Row k="Email" v={user?.email ?? "—"} />
                <Row k="Username" v={user?.username || "—"} />
                <Row k="Role" v={<span className="capitalize">{user?.role ?? "—"}</span>} />
                {user?.organization?.name && <Row k="Organization" v={user.organization.name} />}
                <div className="pt-3">
                  <Button variant="outline" size="sm" onClick={() => logout()}>
                    Sign out
                  </Button>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Access</CardTitle>
              </CardHeader>
              <CardContent>
                {granted.length === 0 ? (
                  <p className="text-[13px] text-muted-foreground">No specific permissions resolved.</p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {granted.map(([k]) => (
                      <Badge key={k} variant={k === "is_admin" ? "brand" : "success"}>
                        {k.replace(/^can_view_/, "").replace(/_/g, " ")}
                      </Badge>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            <WorkspaceDefaults />
          </div>
        </TabsContent>

        <TabsContent value="security">
          <ChangePassword />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-muted-foreground">{k}</span>
      <span className="text-right font-medium">{v}</span>
    </div>
  );
}

/** Field/non-field errors keyed exactly like Django's PasswordChangeForm.errors:
 * per-field lists keyed by field name, plus `__all__` for non-field errors. */
type FormErrors = Record<string, string[]>;

/** Change-password form mirroring the classic User Settings security tab. */
function ChangePassword() {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword1, setNewPassword1] = useState("");
  const [newPassword2, setNewPassword2] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});

  const mutation = useMutation({
    mutationFn: () => api.auth.changePassword(oldPassword, newPassword1, newPassword2),
    onSuccess: () => {
      setErrors({});
      setOldPassword("");
      setNewPassword1("");
      setNewPassword2("");
    },
    onError: (err) => {
      const body = err instanceof ApiError ? (err.body as { errors?: FormErrors } | undefined) : undefined;
      setErrors(body?.errors ?? { __all__: [err instanceof Error ? err.message : "Something went wrong."] });
    },
  });

  const nonFieldErrors = errors.__all__ ?? [];

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle>Change password</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            mutation.mutate();
          }}
        >
          {nonFieldErrors.length > 0 && (
            <div className="rounded-md border border-err/30 bg-err/10 px-3 py-2 text-[13px] text-err">
              {nonFieldErrors.map((msg, i) => (
                <p key={i}>{msg}</p>
              ))}
            </div>
          )}

          <PasswordField
            label="Current password"
            value={oldPassword}
            onChange={setOldPassword}
            errors={errors.old_password}
            autoComplete="current-password"
          />
          <PasswordField
            label="New password"
            value={newPassword1}
            onChange={setNewPassword1}
            errors={errors.new_password1}
            autoComplete="new-password"
          />
          <PasswordField
            label="Confirm new password"
            value={newPassword2}
            onChange={setNewPassword2}
            errors={errors.new_password2}
            autoComplete="new-password"
          />

          <div className="flex items-center gap-3 pt-1">
            <Button type="submit" variant="brand" size="sm" disabled={mutation.isPending}>
              {mutation.isPending ? "Updating…" : "Update password"}
            </Button>
            {mutation.isSuccess && <span className="text-[12.5px] text-ok">Password updated.</span>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function PasswordField({
  label,
  value,
  onChange,
  errors,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  errors?: string[];
  autoComplete?: string;
}) {
  return (
    <div className="space-y-1.5">
      <label className="block text-[13px] font-medium text-foreground">{label}</label>
      <Input
        type="password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        className={errors?.length ? "border-err focus-visible:ring-err" : undefined}
      />
      {errors?.length ? (
        <ul className="space-y-0.5 text-[12px] text-err">
          {errors.map((msg, i) => (
            <li key={i}>{msg}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
