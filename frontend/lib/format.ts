/** Shared display formatters for admin/integration views. */

/** ISO timestamp -> compact local "Jun 26, 14:05" (or "—"). */
export function fmtWhen(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Seconds -> "45s" / "3m 12s" / "1h 4m" (or "—"). */
export function fmtDuration(sec?: number | null): string {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return "—";
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
