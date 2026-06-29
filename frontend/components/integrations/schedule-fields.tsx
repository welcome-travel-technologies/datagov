"use client";

import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { FREQUENCIES, WEEKDAYS, type ScheduleState } from "@/lib/integrations/schedule";

export function ScheduleFields({
  value,
  onChange,
}: {
  value: ScheduleState;
  onChange: (s: ScheduleState) => void;
}) {
  const set = (patch: Partial<ScheduleState>) => onChange({ ...value, ...patch });
  const timed = value.frequency === "daily" || value.frequency === "weekly";

  return (
    <div className="space-y-3 rounded-lg border border-line bg-panel2/40 p-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[12.5px] font-medium">Scheduled runs</div>
          <div className="text-[11.5px] text-muted-foreground">Run automatically in the background.</div>
        </div>
        <Switch checked={value.enabled} onCheckedChange={(v) => set({ enabled: v })} />
      </div>

      {value.enabled && (
        <div className="space-y-2.5">
          <div className="grid grid-cols-2 gap-2">
            <Labeled label="Frequency">
              <Select value={value.frequency} onValueChange={(v) => set({ frequency: v })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FREQUENCIES.map((f) => (
                    <SelectItem key={f.value} value={f.value}>
                      {f.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Labeled>
            {timed && (
              <Labeled label="Hour (0–23)">
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={value.hour}
                  onChange={(e) => set({ hour: e.target.value })}
                />
              </Labeled>
            )}
          </div>

          {value.frequency === "weekly" && (
            <Labeled label="Day of week">
              <Select value={value.day} onValueChange={(v) => set({ day: v })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WEEKDAYS.map((d) => (
                    <SelectItem key={d.value} value={d.value}>
                      {d.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Labeled>
          )}

          {value.frequency === "custom" && (
            <Labeled label="Cron expression">
              <Input
                value={value.cron}
                onChange={(e) => set({ cron: e.target.value })}
                placeholder="0 2 * * *"
              />
            </Labeled>
          )}
        </div>
      )}
    </div>
  );
}

function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">{label}</span>
      {children}
    </label>
  );
}
