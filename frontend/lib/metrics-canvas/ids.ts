/** Short unique id for canvas nodes / edges / groups (parity with source `uid`). */
let _seq = 0;

export function uid(prefix = "id"): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}_${crypto.randomUUID().slice(0, 8)}`;
  }
  _seq += 1;
  return `${prefix}_${_seq.toString(36)}${Math.floor((typeof performance !== "undefined" ? performance.now() : _seq) * 1000).toString(36)}`;
}
