/**
 * Geometry for "floating" canvas edges — the React Flow floating-edges recipe.
 *
 * Instead of gluing an edge to a fixed port (top/right/bottom/left), we find the
 * point on each node's border that faces the *other* node and attach there. The
 * result routes cleanly however the boxes are arranged or whichever handle the
 * edge was originally drawn from. Only the attachment geometry is derived here;
 * which nodes are connected (and the arrow direction) is untouched.
 */
import { Position, type InternalNode, type Node } from "@xyflow/react";

type IN = InternalNode<Node>;

function sizeOf(node: IN): { w: number; h: number } {
  return {
    w: node.measured?.width ?? (node.width as number | undefined) ?? 190,
    h: node.measured?.height ?? (node.height as number | undefined) ?? 60,
  };
}

/** Point where the center-to-center line crosses `node`'s rectangle border. */
function intersection(node: IN, other: IN): { x: number; y: number } {
  const { w: nw, h: nh } = sizeOf(node);
  const { w: ow, h: oh } = sizeOf(other);
  const w = nw / 2;
  const h = nh / 2;

  const nx2 = node.internals.positionAbsolute.x + w;
  const ny2 = node.internals.positionAbsolute.y + h;
  const ox = other.internals.positionAbsolute.x + ow / 2;
  const oy = other.internals.positionAbsolute.y + oh / 2;

  const xx1 = (ox - nx2) / (2 * w) - (oy - ny2) / (2 * h);
  const yy1 = (ox - nx2) / (2 * w) + (oy - ny2) / (2 * h);
  const a = 1 / (Math.abs(xx1) + Math.abs(yy1) || 1);
  const xx3 = a * xx1;
  const yy3 = a * yy1;
  const x = w * (xx3 + yy3) + nx2;
  const y = h * (-xx3 + yy3) + ny2;
  return { x, y };
}

/** Which border of `node` the intersection point sits on. */
function sideOf(node: IN, point: { x: number; y: number }): Position {
  const { w, h } = sizeOf(node);
  const nx = Math.round(node.internals.positionAbsolute.x);
  const ny = Math.round(node.internals.positionAbsolute.y);
  const px = Math.round(point.x);
  const py = Math.round(point.y);
  if (px <= nx + 1) return Position.Left;
  if (px >= nx + Math.round(w) - 1) return Position.Right;
  if (py <= ny + 1) return Position.Top;
  return Position.Bottom;
}

export interface EdgeGeometry {
  sx: number;
  sy: number;
  tx: number;
  ty: number;
  sourcePos: Position;
  targetPos: Position;
}

/** Attachment points + facing sides for an edge between two internal nodes. */
export function getEdgeParams(source: IN, target: IN): EdgeGeometry {
  const sp = intersection(source, target);
  const tp = intersection(target, source);
  return {
    sx: sp.x,
    sy: sp.y,
    tx: tp.x,
    ty: tp.y,
    sourcePos: sideOf(source, sp),
    targetPos: sideOf(target, tp),
  };
}
