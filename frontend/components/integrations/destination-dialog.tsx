"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Save } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Field, FormError } from "@/components/ui/form-field";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { ScheduleFields } from "@/components/integrations/schedule-fields";
import {
  scheduleStateFrom,
  schedulePayload,
  type ScheduleState,
} from "@/lib/integrations/schedule";
import { api, getApiErrorMessage, type DestinationInput, type IntegrationDestination } from "@/lib/api";

export function DestinationDialog({
  open,
  onOpenChange,
  destination,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  destination: IntegrationDestination;
  onSaved: () => void;
}) {
  const [name, setName] = useState(destination.name);
  const [datasetId, setDatasetId] = useState(destination.bq_dataset_id);
  const [saJson, setSaJson] = useState("");
  const [schedule, setSchedule] = useState<ScheduleState>(scheduleStateFrom(destination.schedule));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(destination.name);
    setDatasetId(destination.bq_dataset_id);
    setSaJson("");
    setSchedule(scheduleStateFrom(destination.schedule));
    setError(null);
  }, [open, destination]);

  const saveMut = useMutation({
    mutationFn: () => {
      const body: DestinationInput = {
        id: destination.id,
        name,
        bq_dataset_id: datasetId,
        ...schedulePayload(schedule),
      };
      if (saJson.trim()) body.bq_service_account_json = saJson.trim();
      return api.integrations.saveDestination(body);
    },
    onSuccess: () => {
      onSaved();
      onOpenChange(false);
    },
    onError: (e) => setError(getApiErrorMessage(e, "Could not save destination.")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Configure {destination.name}</DialogTitle>
          <DialogDescription>
            BigQuery dataset and service-account credentials used to push catalog metadata.
            The project ID is read from the service-account JSON.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Field label="Display name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="BigQuery dataset ID">
            <Input
              value={datasetId}
              onChange={(e) => setDatasetId(e.target.value)}
              placeholder="my_dataset"
            />
          </Field>
          {destination.bq_project_id && (
            <p className="text-[12px] text-muted-foreground">
              Current project: <span className="font-medium text-foreground">{destination.bq_project_id}</span>
            </p>
          )}
          <Field
            label={
              destination.bq_service_account_set
                ? "Service account JSON (set — blank keeps it)"
                : "Service account JSON"
            }
          >
            <textarea
              value={saJson}
              onChange={(e) => setSaJson(e.target.value)}
              rows={5}
              placeholder={
                destination.bq_service_account_set
                  ? "•••• configured — paste new JSON to replace"
                  : '{ "type": "service_account", "project_id": "…" }'
              }
              className={cn(
                "flex w-full rounded-md border border-input bg-panel px-3 py-2 font-mono text-[12px] shadow-sm",
                "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              )}
            />
          </Field>

          <ScheduleFields value={schedule} onChange={setSchedule} />

          {error && <FormError>{error}</FormError>}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="brand" onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            <Save /> {saveMut.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
