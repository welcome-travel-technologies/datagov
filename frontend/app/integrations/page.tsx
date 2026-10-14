"use client";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/misc";
import { IntegrationsManager } from "@/components/integrations/integrations-manager";
import { IntegrationContent } from "@/components/integrations/integration-content";
import { useAuth } from "@/lib/auth";

export default function IntegrationsPage() {
  const { user } = useAuth();
  const canManage = !!(user?.perms?.is_admin || user?.perms?.can_view_integrations);

  return (
    <div>
      <PageHeader
        title="Integrations"
        description="Connect systems, manage data and documents, and monitor your pipelines."
      />
      {canManage ? (
        <IntegrationsManager />
      ) : user?.perms?.can_view_chat ? <IntegrationContent /> : (
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              title="Admin access required"
              hint="Integrations management is available to organization admins only."
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
