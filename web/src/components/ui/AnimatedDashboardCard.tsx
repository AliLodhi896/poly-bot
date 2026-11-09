"use client";

import { motion, useReducedMotion } from "motion/react";
import { AnimatedNumber } from "@/components/AnimatedNumber";
import { cn } from "@/lib/utils";

// Inspired by 21st.dev isaiahbjork/animated-dashboard-card: two financial metrics beside
// animated concentric rings of dots. Themed gold-on-midnight; respects reduced-motion.
interface AnimatedDashboardCardProps {
  title?: string;
  primaryLabel: string;
  primaryValue: number | null;
  primaryFormat: (n: number) => string;
  primaryTone?: string;
  secondaryLabel: string;
  secondaryValue: number | null;
  secondaryFormat: (n: number) => string;
  badge?: string;
  className?: string;
}

function ring(count: number, radius: number, cx: number, cy: number) {
  return Array.from({ length: count }, (_, i) => {
    const a = (i / count) * Math.PI * 2;
    return { x: cx + radius * Math.cos(a), y: cy + radius * Math.sin(a), i };
  });
}

export function AnimatedDashboardCard({
  title = "Live performance",
  primaryLabel,
  primaryValue,
  primaryFormat,
  primaryTone = "text-foreground",
  secondaryLabel,
  secondaryValue,
  secondaryFormat,
  badge,
  className,
}: AnimatedDashboardCardProps) {
  const reduce = useReducedMotion();
  const outer = ring(40, 92, 110, 110);
  const inner = ring(28, 60, 110, 110);

  return (
    <motion.section
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "relative overflow-hidden rounded-2xl border border-line bg-base/40 p-6 backdrop-blur-[2px] sm:p-8",
        className,
      )}
    >
      {/* soft color blobs */}
      <div className="pointer-events-none absolute -left-10 -top-10 h-40 w-40 rounded-full bg-gold/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-12 right-24 h-44 w-44 rounded-full bg-profit/10 blur-3xl" />

      <div className="relative flex flex-col items-start justify-between gap-8 sm:flex-row sm:items-center">
        {/* metrics */}
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full bg-profit live-dot" />
            <span className="eyebrow text-muted">{title}</span>
            {badge && (
              <span className="rounded border border-line-strong bg-surface px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                {badge}
              </span>
            )}
          </div>

          <div className="mt-4">
            <span className="eyebrow">{primaryLabel}</span>
            <div
              className={cn(
                "mt-1 tnum text-[clamp(40px,6vw,64px)] font-semibold leading-none",
                primaryTone,
              )}
            >
              {primaryValue === null ? (
                "—"
              ) : (
                <AnimatedNumber value={primaryValue} format={primaryFormat} />
              )}
            </div>
          </div>

          <div className="mt-6 inline-flex items-baseline gap-3 border-t border-line/70 pt-3">
            <span className="eyebrow">{secondaryLabel}</span>
            <span className="tnum text-[20px] font-medium text-foreground">
              {secondaryValue === null ? (
                "—"
              ) : (
                <AnimatedNumber value={secondaryValue} format={secondaryFormat} />
              )}
            </span>
          </div>
        </div>

        {/* animated dot rings */}
        <svg
          viewBox="0 0 220 220"
          className="h-44 w-44 shrink-0 sm:h-52 sm:w-52"
          aria-hidden
        >
          <circle cx="110" cy="110" r="92" fill="none" stroke="var(--color-line)" strokeWidth="1" opacity="0.5" />
          <circle cx="110" cy="110" r="60" fill="none" stroke="var(--color-line)" strokeWidth="1" opacity="0.4" />
          {outer.map((d) => (
            <motion.circle
              key={`o${d.i}`}
              cx={d.x}
              cy={d.y}
              r="1.7"
              fill="var(--color-gold)"
              initial={{ opacity: 0.25 }}
              animate={reduce ? { opacity: 0.45 } : { opacity: [0.2, 1, 0.2] }}
              transition={
                reduce
                  ? undefined
                  : { duration: 2.4, repeat: Infinity, ease: "easeInOut", delay: (d.i / outer.length) * 2.4 }
              }
            />
          ))}
          {inner.map((d) => (
            <motion.circle
              key={`i${d.i}`}
              cx={d.x}
              cy={d.y}
              r="1.4"
              fill="var(--color-profit)"
              initial={{ opacity: 0.2 }}
              animate={reduce ? { opacity: 0.4 } : { opacity: [0.15, 0.8, 0.15] }}
              transition={
                reduce
                  ? undefined
                  : {
                      duration: 2.8,
                      repeat: Infinity,
                      ease: "easeInOut",
                      delay: (d.i / inner.length) * 2.8,
                    }
              }
            />
          ))}
          <circle cx="110" cy="110" r="3" fill="var(--color-gold)" />
        </svg>
      </div>
    </motion.section>
  );
}
