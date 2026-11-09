"use client";

import { motion } from "motion/react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

interface SectionProps {
  title: string;
  count?: number;
  right?: ReactNode;
  className?: string;
  children: ReactNode;
}

export function Section({ title, count, right, className, children }: SectionProps) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: "easeOut" }}
      className={cn(
        "flex min-h-0 flex-col overflow-hidden border-y border-line/65 bg-base/24 backdrop-blur-[2px]",
        className,
      )}
    >
      <header className="flex items-center justify-between gap-3 border-b border-line/70 px-4 py-3">
        <div className="flex items-baseline gap-2">
          <h2 className="eyebrow !tracking-[0.18em] text-muted">{title}</h2>
          {count !== undefined && <span className="tnum text-[11px] text-faint">{count}</span>}
        </div>
        {right}
      </header>
      <div className="min-h-0 flex-1">{children}</div>
    </motion.section>
  );
}
