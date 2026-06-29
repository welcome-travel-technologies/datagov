"use client";

import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { PageHeader } from "@/components/page-header";
import { MetricsCanvas } from "@/components/metrics-canvas/metrics-canvas";

export default function MetricsMapPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Metrics Map"
        description="Map your metrics visually — drag tables, measures, columns and pages straight from your catalog onto a canvas and connect them into a diagram."
      />
      <ReactFlowProvider>
        <MetricsCanvas />
      </ReactFlowProvider>
    </div>
  );
}
