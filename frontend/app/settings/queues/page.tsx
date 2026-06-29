"use client";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/misc";
import { OrgQueuesPanel } from "@/components/org/org-queues-panel";
import { useAuth } from "@/lib/auth";

export default function QueuesPage() {
  const { user } = useAuth();
  const canManage = !!user?.perms?.can_view_org_settings || !!user?.perms?.is_admin;

  return (
    <div>
      <PageHeader
        title="Queues"
        description="Background job queue — what's waiting, what's scheduled, and recent run outcomes from the Django Q worker."
      />
      {canManage ? (
        <OrgQueuesPanel />
      ) : (
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              title="Admin access required"
              hint="The job queue is available to organization admins only."
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
