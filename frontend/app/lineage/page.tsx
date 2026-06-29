"use client";

import { Suspense } from "react";
import "@xyflow/react/dist/style.css";
import { LineagePage } from "@/components/lineage/lineage-page";
import { LoadingState } from "@/components/ui/misc";

export default function LineageRoute() {
  return (
    <div className="flex h-full flex-col space-y-4">
      <h1 className="text-[22px] font-semibold -tracking-[0.02em]">Data Lineage</h1>

      <Suspense fallback={<LoadingState />}>
        <LineagePage />
      </Suspense>
    </div>
  );
}
