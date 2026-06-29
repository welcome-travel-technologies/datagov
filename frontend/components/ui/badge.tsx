import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2.5 h-[22px] text-[11.5px] font-semibold -tracking-[0.01em]",
  {
    variants: {
      variant: {
        default: "bg-muted-foreground/12 text-muted-foreground",
        brand: "bg-brand/15 text-brand",
        success: "bg-ok/15 text-ok",
        warning: "bg-warn/15 text-warn",
        danger: "bg-err/15 text-err",
        info: "bg-run/15 text-run",
        outline: "border border-line-strong text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  dot?: boolean;
}

export function Badge({ className, variant, dot, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props}>
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

export { badgeVariants };
