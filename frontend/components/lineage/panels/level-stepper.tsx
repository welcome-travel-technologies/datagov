"use client";

import { ArrowLeft, ArrowRight, Minus, Plus } from "lucide-react";
import { cn } from "@/lib/utils";

/** Backend BFS clamp (views.get_network clamps depth to 0..5). */
const MAX_DEPTH = 5;

export interface LevelStepperProps {
  upDepth: number;
  downDepth: number;
  disabled?: boolean;
  /** Called with the requested radii; the orchestrator refetches the graph. */
  onChange: (up: number, down: number) => void;
}

/**
 * Gradual lineage navigation: how many levels are loaded upstream (left) and
 * downstream (right) of the focused element. Each ± steps one level in that
 * direction and re-lays-out the whole canvas, so growing/shrinking the graph
 * is always a single predictable click.
 */
export function LevelStepper({ upDepth, downDepth, disabled, onChange }: LevelStepperProps) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-line bg-panel/95 px-1.5 py-1 shadow-card backdrop-blur">
      <Side
        icon={<ArrowLeft className="h-3 w-3" />}
        label="Upstream"
        value={upDepth}
        disabled={disabled}
        onStep={(d) => onChange(Math.min(MAX_DEPTH, Math.max(0, upDepth + d)), downDepth)}
      />
      <span className="mx-1 h-4 w-px bg-line" />
      <Side
        icon={<ArrowRight className="h-3 w-3" />}
        label="Downstream"
        value={downDepth}
        disabled={disabled}
        onStep={(d) => onChange(upDepth, Math.min(MAX_DEPTH, Math.max(0, downDepth + d)))}
      />
    </div>
  );
}

function Side({
  icon,
  label,
  value,
  disabled,
  onStep,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  disabled?: boolean;
  onStep: (delta: 1 | -1) => void;
}) {
  return (
    <div className="flex items-center gap-0.5" title={`${label} levels loaded around the focused element`}>
      <span className="flex items-center gap-1 pl-1 pr-0.5 text-[11px] text-faint">
        {icon}
        {label}
      </span>
      <StepButton
        title={`One less ${label.toLowerCase()} level`}
        disabled={disabled || value <= 0}
        onClick={() => onStep(-1)}
      >
        <Minus className="h-3 w-3" />
      </StepButton>
      <span className="w-4 text-center text-[12px] font-semibold tabular-nums">{value}</span>
      <StepButton
        title={`One more ${label.toLowerCase()} level`}
        disabled={disabled || value >= MAX_DEPTH}
        onClick={() => onStep(1)}
      >
        <Plus className="h-3 w-3" />
      </StepButton>
    </div>
  );
}

function StepButton({
  title,
  disabled,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "grid h-5 w-5 place-items-center rounded border border-line text-faint",
        "hover:border-brand hover:text-brand disabled:cursor-not-allowed disabled:opacity-30",
      )}
    >
      {children}
    </button>
  );
}
