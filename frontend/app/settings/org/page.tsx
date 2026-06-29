"use client";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/misc";
import { OrgSettingsManager } from "@/components/org/org-settings-manager";
import { useAuth } from "@/lib/auth";

export default function OrgSettingsPage() {
  const { user } = useAuth();
  const canManage = !!user?.perms?.can_view_org_settings;

  return (
    <div>
      <PageHeader
        title="Org Settings"
        description="Manage members, page access, governance roles, and AI Assistant settings."
      />
      {canManage ? (
        <OrgSettingsManager />
      ) : (
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              title="Admin access required"
              hint="Organization Settings is available to organization admins only."
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
