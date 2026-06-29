"use client";

import * as React from "react";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
  SelectGroup,
  SelectLabel,
} from "@/components/ui/select";

export interface SelectOption {
  value: string;
  label: React.ReactNode;
  disabled?: boolean;
}

export interface SelectOptionGroup {
  label: string;
  options: SelectOption[];
}

/** Radix forbids a `<SelectItem>` with an empty-string value, but our filters use
 * "" as the "All / none" sentinel. Map it to a private token at the boundary so
 * callers keep working with plain "". */
const EMPTY = "⁣__empty__";
const toRadix = (v: string) => (v === "" ? EMPTY : v);
const fromRadix = (v: string) => (v === EMPTY ? "" : v);

/**
 * Drop-in replacement for a native `<select>` built on the styled Radix Select,
 * so every dropdown in the app gets the same rounded, spaced popover instead of
 * the OS-drawn rectangular menu. Pass either flat `options` or grouped `groups`.
 */
export function SimpleSelect({
  value,
  onValueChange,
  options,
  groups,
  placeholder,
  className,
  contentClassName,
  title,
  disabled,
  "aria-label": ariaLabel,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options?: SelectOption[];
  groups?: SelectOptionGroup[];
  placeholder?: string;
  /** Classes for the trigger button (e.g. width / height overrides). */
  className?: string;
  contentClassName?: string;
  title?: string;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const renderItem = (o: SelectOption) => (
    <SelectItem key={o.value} value={toRadix(o.value)} disabled={o.disabled}>
      {o.label}
    </SelectItem>
  );

  return (
    <Select value={toRadix(value)} onValueChange={(v) => onValueChange(fromRadix(v))} disabled={disabled}>
      <SelectTrigger className={className} title={title} aria-label={ariaLabel}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent className={contentClassName}>
        {groups
          ? groups.map((g) => (
              <SelectGroup key={g.label}>
                <SelectLabel>{g.label}</SelectLabel>
                {g.options.map(renderItem)}
              </SelectGroup>
            ))
          : options?.map(renderItem)}
      </SelectContent>
    </Select>
  );
}
