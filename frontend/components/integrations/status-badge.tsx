import { Badge, type BadgeProps } from "@/components/ui/badge";

/** Map an integration run/workflow status string to a Badge variant. */
export function statusVariant(status?: string | null): NonNullable<BadgeProps["variant"]> {
  switch ((status || "").toLowerCase()) {
    case "success":
    case "completed":
    case "ok":
      return "success";
    case "running":
    case "pending":
    case "queued":
      return "info";
    case "failed":
    case "error":
      return "danger";
    default:
      return "outline";
  }
}

export function StatusBadge({ status }: { status?: string | null }) {
  const label = status ? status[0].toUpperCase() + status.slice(1) : "Never run";
  return (
    <Badge variant={statusVariant(status)} dot>
      {label}
    </Badge>
  );
}

/** True while a run is in a non-terminal state (drives polling/disabled buttons). */
export function isActiveStatus(status?: string | null): boolean {
  const s = (status || "").toLowerCase();
  return s === "running" || s === "pending" || s === "queued";
}
