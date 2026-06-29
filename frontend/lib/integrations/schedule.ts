import type { IntegrationSchedule } from "@/lib/api";

/** UI-local schedule editing state (mapped to the backend's friendly fields). */
export interface ScheduleState {
  frequency: string; // manual | daily | weekly | custom
  enabled: boolean;
  hour: string; // "0".."23"
  day: string; // cron day-of-week, 1=Mon .. 0=Sun
  cron: string; // used when frequency === "custom"
}

export const FREQUENCIES = [
  { value: "manual", label: "Manual only" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "custom", label: "Custom cron" },
];

export const WEEKDAYS = [
  { value: "1", label: "Monday" },
  { value: "2", label: "Tuesday" },
  { value: "3", label: "Wednesday" },
  { value: "4", label: "Thursday" },
  { value: "5", label: "Friday" },
  { value: "6", label: "Saturday" },
  { value: "0", label: "Sunday" },
];

export function scheduleStateFrom(s: IntegrationSchedule | null): ScheduleState {
  const base: ScheduleState = { frequency: "manual", enabled: false, hour: "2", day: "1", cron: "" };
  if (!s) return base;
  // Best-effort: recover hour/day from a "m h * * d" cron so the friendly
  // controls reflect a previously-saved schedule.
  const parts = (s.cron_expression || "").trim().split(/\s+/);
  if (parts.length === 5) {
    if (/^\d+$/.test(parts[1])) base.hour = parts[1];
    if (/^\d+$/.test(parts[4])) base.day = parts[4];
  }
  return {
    ...base,
    frequency: s.frequency || "manual",
    enabled: s.is_enabled,
    cron: s.cron_expression || "",
  };
}

/** Short human label for a schedule, e.g. "Daily", "Weekly", "Cron: 0 2 * * *". */
export function describeSchedule(s: IntegrationSchedule | null): string {
  if (!s || !s.is_enabled) return "Manual";
  if (s.frequency === "custom") return `Cron: ${s.cron_expression || "—"}`;
  if (s.frequency === "daily") return "Daily";
  if (s.frequency === "weekly") return "Weekly";
  return s.frequency || "Manual";
}

/** Map editing state to the {schedule_*} fields the source/destination save
 * endpoints expect (the backend turns these into a cron string). */
export function schedulePayload(s: ScheduleState) {
  return {
    schedule_enabled: s.enabled,
    schedule_frequency: s.frequency,
    schedule_cron: s.cron,
    schedule_hour: s.hour,
    schedule_day: s.day,
  };
}
