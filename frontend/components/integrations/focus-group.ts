"use client";

import { useEffect, useRef } from "react";

/**
 * The source group an overview "Manage" link points at (`?group=<key>`). Once
 * that group has rendered inside the returned container (mark each group's
 * element with `data-group-key`), its `<details>` opens and it scrolls into
 * view: once per group, so a refetch or the reader collapsing it again is left
 * alone. `keys` are the group keys the component knows about.
 */
export function useFocusGroup<T extends HTMLElement = HTMLDivElement>(group: string | undefined, keys: readonly string[]) {
  const container = useRef<T>(null);
  const handled = useRef("");
  // Checked after every render until done: the group can appear in the data
  // before the container itself is on screen (a loading state in between).
  useEffect(() => {
    if (!group || handled.current === group || !keys.includes(group) || !container.current) return;
    const target = Array.from(container.current.querySelectorAll<HTMLElement>("[data-group-key]"))
      .find((element) => element.dataset.groupKey === group);
    if (!target) return;
    handled.current = group;
    const details = target instanceof HTMLDetailsElement ? target : target.querySelector("details");
    if (details) details.open = true;
    target.scrollIntoView?.({ behavior: "smooth", block: "start" });
  });
  return container;
}
