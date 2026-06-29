"use client";

import { PageHeader } from "@/components/page-header";
import { DashboardView } from "@/components/dashboard/dashboard-view";

export default function DashboardPage() {
  return (
    <div>
      <PageHeader
        title="Dashboard Overview"
        description="A high-level view of your Power BI environments and measure governance."
      />
      <DashboardView />
    </div>
  );
}
