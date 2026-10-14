"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, getApiErrorMessage, type IntegrationSchedule } from "@/lib/api";
import { schedulePayload, scheduleStateFrom } from "@/lib/integrations/schedule";
import { ScheduleFields } from "./schedule-fields";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

export function StepScheduleDialog({ step, kind, onClose }: {
  step: { id: number; name: string; schedule: IntegrationSchedule | null };
  kind: "source" | "destination"; onClose: () => void;
}) {
  const [schedule, setSchedule] = useState(() => scheduleStateFrom(step.schedule));
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: () => (kind === "source" ? api.integrations.saveSource : api.integrations.saveDestination)({ id: step.id, ...schedulePayload(schedule) }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["integrations"] }); void qc.invalidateQueries({ queryKey: ["workflow-status"] }); onClose(); },
  });
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader>
    <DialogTitle>Schedule: {step.name}</DialogTitle><DialogDescription>This independent schedule runs this step. Use the catalog workflow schedule to run sources and destinations in order.</DialogDescription>
  </DialogHeader><ScheduleFields value={schedule} onChange={setSchedule} />
    {save.isError && <p role="alert" className="text-sm text-err">{getApiErrorMessage(save.error, "Schedule could not be saved.")}</p>}
    <div className="flex justify-end gap-2"><Button variant="outline" onClick={onClose}>Cancel</Button><Button variant="brand" disabled={save.isPending} onClick={() => save.mutate()}>Save schedule</Button></div>
  </DialogContent></Dialog>;
}
