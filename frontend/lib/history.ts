/**
 * Generic undo/redo snapshot stack shared by the lineage and metrics-canvas
 * editors. A snapshot is any JSON-serializable value — the caller passes a
 * serializable slice of its state (e.g. Sets flattened to arrays).
 *
 * Contract: call `push(current)` BEFORE a mutating action; `undo`/`redo` take the
 * CURRENT snapshot and return the snapshot to restore (or null when empty).
 */
import { useCallback, useMemo, useRef, useState } from "react";

export interface History<T> {
  canUndo: boolean;
  canRedo: boolean;
  push: (snap: T) => void;
  undo: (current: T) => T | null;
  redo: (current: T) => T | null;
  reset: () => void;
}

const DEFAULT_MAX = 50;

export function useHistory<T>(max: number = DEFAULT_MAX): History<T> {
  const past = useRef<string[]>([]);
  const future = useRef<string[]>([]);
  const [, bump] = useState(0);
  const tick = useCallback(() => bump((n) => n + 1), []);

  const push = useCallback(
    (snap: T) => {
      past.current.push(JSON.stringify(snap));
      if (past.current.length > max) past.current.shift();
      future.current = [];
      tick();
    },
    [max, tick],
  );

  const undo = useCallback(
    (current: T): T | null => {
      if (!past.current.length) return null;
      future.current.push(JSON.stringify(current));
      const prev = past.current.pop()!;
      tick();
      return JSON.parse(prev) as T;
    },
    [tick],
  );

  const redo = useCallback(
    (current: T): T | null => {
      if (!future.current.length) return null;
      past.current.push(JSON.stringify(current));
      const next = future.current.pop()!;
      tick();
      return JSON.parse(next) as T;
    },
    [tick],
  );

  const reset = useCallback(() => {
    past.current = [];
    future.current = [];
    tick();
  }, [tick]);

  // Stable object identity except when a mutation bumps the version, so callers
  // can safely depend on it without triggering render loops.
  const version = past.current.length * 1000 + future.current.length;
  return useMemo(
    () => ({
      canUndo: past.current.length > 0,
      canRedo: future.current.length > 0,
      push,
      undo,
      redo,
      reset,
    }),
    [push, undo, redo, reset, version],
  );
}
