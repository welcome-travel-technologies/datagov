"use client";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/misc";
import { IntegrationsManager } from "@/components/integrations/integrations-manager";
import { useAuth } from "@/lib/auth";

export default function IntegrationsPage() {
  const { user } = useAuth();
  const canManage = !!user?.perms?.can_view_integrations;

  return (
    <div>
      <PageHeader
        title="Integrations"
        description="Run, test, and schedule the PowerBI / Fabric and dbt ETL pipeline syncing to BigQuery."
      />
      {canManage ? (
        <IntegrationsManager />
      ) : (
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
