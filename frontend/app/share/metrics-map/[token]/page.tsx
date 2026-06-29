"use client";

import { use } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { MetricsCanvasViewer } from "@/components/metrics-canvas/metrics-canvas-viewer";

/**
 * Public share page for a metrics map. Lives outside the authenticated app
 * shell (see AuthGuard PUBLIC_PATHS + AppShell) so anyone with the link — logged
 * in or not, any org — gets a full-screen, read-only view.
 */
export default function SharedMetricsMapPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  return (
    <ReactFlowProvider>
      <MetricsCanvasViewer token={token} />
    </ReactFlowProvider>
  );
}
