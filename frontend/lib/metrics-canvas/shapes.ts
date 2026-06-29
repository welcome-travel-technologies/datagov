/**
 * SVG shape geometry for the canvas "Shapes" node type — ported verbatim from
 * the source `metrics-map.html` `buildShapeSVG` / `hexToRgba` (lines 1304-1343).
 * Returns an SVG-fragment string sized to the node box, rendered into a node's
 * `<svg>` via `dangerouslySetInnerHTML`.
 */
import type { ShapeKey } from "@/lib/metrics-canvas/catalog";

export function buildShapeSVG(
  shape: ShapeKey | string,
  w: number,
  h: number,
  fill: string,
  stroke: string,
  sw = 2,
): string {
  const i = sw / 2;
  switch (shape) {
    case "cylinder": {
      const ry = Math.min(20, h * 0.18);
      return `<path d="M ${i} ${ry} A ${(w - sw) / 2} ${ry} 0 0 1 ${w - i} ${ry} L ${w - i} ${h - ry} A ${(w - sw) / 2} ${ry} 0 0 1 ${i} ${h - ry} Z" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/><ellipse cx="${w / 2}" cy="${ry}" rx="${(w - sw) / 2}" ry="${ry}" fill="none" stroke="${stroke}" stroke-width="${sw}"/>`;
    }
    case "document": {
      const wave = Math.max(8, h * 0.1);
      return `<path d="M ${i} ${i} L ${w - i} ${i} L ${w - i} ${h - wave} Q ${w * 0.75} ${h - wave * 2} ${w / 2} ${h - wave} T ${i} ${h - wave} Z" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>`;
    }
    case "cloud": {
      return `<path d="M ${w * 0.22} ${h * 0.78} C ${w * 0.05} ${h * 0.78} ${w * 0.05} ${h * 0.45} ${w * 0.22} ${h * 0.4} C ${w * 0.22} ${h * 0.15} ${w * 0.52} ${h * 0.1} ${w * 0.58} ${h * 0.32} C ${w * 0.7} ${h * 0.1} ${w * 0.97} ${h * 0.25} ${w * 0.86} ${h * 0.45} C ${w * 1.0} ${h * 0.5} ${w * 0.96} ${h * 0.82} ${w * 0.78} ${h * 0.78} Z" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>`;
    }
    case "diamond": {
      const p = `${w / 2},${i} ${w - i},${h / 2} ${w / 2},${h - i} ${i},${h / 2}`;
      return `<polygon points="${p}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>`;
    }
    case "ellipse":
      return `<ellipse cx="${w / 2}" cy="${h / 2}" rx="${(w - sw) / 2}" ry="${(h - sw) / 2}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
    case "hexagon": {
      const dx = w * 0.18;
      const p = `${dx},${i} ${w - dx},${i} ${w - i},${h / 2} ${w - dx},${h - i} ${dx},${h - i} ${i},${h / 2}`;
      return `<polygon points="${p}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>`;
    }
  }
  return "";
}

/** `#rrggbb` (or `#rgb`) → `rgba(...)` with the given alpha; passes through non-hex. */
export function hexToRgba(hex: string | undefined | null, alpha = 1): string {
  if (!hex || typeof hex !== "string") return "";
  if (!hex.startsWith("#")) return hex;
  let h = hex.slice(1);
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  if (h.length !== 6) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
