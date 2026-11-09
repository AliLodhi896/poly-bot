"use client";

import { motion } from "motion/react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Premium CTA: a slowly rotating conic-gradient rim (the "stardust" edge) around a dark glass
 * fill. On hover the fill brightens, the rim glows, and the button lifts — so it reads as clearly
 * interactive and never blends into the card behind it. */
export function StardustButton({
  children,
  onClick,
  disabled = false,
  busy = false,
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  busy?: boolean;
  className?: string;
}) {
  const off = disabled || busy;
  return (
    <motion.button
      type="button"
      onClick={onClick}
      disabled={off}
      whileHover={off ? undefined : { scale: 1.025, y: -1 }}
      whileTap={off ? undefined : { scale: 0.98 }}
      transition={{ type: "spring", stiffness: 400, damping: 26 }}
      className={cn(
        "group relative inline-flex w-full items-center justify-center overflow-hidden rounded-full px-8 py-3.5 text-[14px] font-medium tracking-wide",
        off ? "cursor-not-allowed text-faint" : "cursor-pointer text-foreground",
        className,
      )}
    >
      {/* rotating conic rim */}
      <span
        aria-hidden
        className="pointer-events-none absolute -inset-[60%] transition-opacity duration-300"
        style={{
          background:
            "conic-gradient(from 0deg, transparent 0deg, rgba(91,140,255,0.95) 40deg, rgba(157,139,255,0.6) 90deg, transparent 150deg, transparent 360deg)",
          animation: off ? undefined : "spin-360 4.2s linear infinite",
          opacity: off ? 0.25 : 0.85,
        }}
      />
      {!off && (
        <span
          aria-hidden
          className="pointer-events-none absolute -inset-[60%] opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          style={{
            background:
              "conic-gradient(from 0deg, transparent 0deg, rgba(120,150,255,1) 50deg, rgba(180,165,255,0.85) 110deg, transparent 170deg, transparent 360deg)",
            animation: "spin-360 3s linear infinite",
          }}
        />
      )}
      {/* dark glass fill (masks the rim to a thin ring) */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-[1.5px] rounded-full"
        style={{
          background: "linear-gradient(180deg, #191b21 0%, #0c0d11 100%)",
          boxShadow:
            "inset 0 1px 0 0 rgba(255,255,255,0.10), inset 0 0 0 1px rgba(255,255,255,0.05)",
        }}
      />
      {/* fill brighten on hover */}
      {!off && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-[1.5px] rounded-full opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          style={{
            background:
              "linear-gradient(180deg, rgba(91,140,255,0.30) 0%, rgba(91,140,255,0.10) 55%, transparent 100%)",
          }}
        />
      )}
      {/* outer glow bloom on hover */}
      {!off && (
        <span
          aria-hidden
          className="pointer-events-none absolute -inset-2 rounded-full opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          style={{ boxShadow: "0 14px 44px -8px rgba(91,140,255,0.6)" }}
        />
      )}
      <span className="relative z-10 flex items-center gap-2">
        {!off && (
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_10px_2px_rgba(91,140,255,0.7)] transition-all duration-300 group-hover:shadow-[0_0_14px_3px_rgba(120,150,255,0.95)]" />
        )}
        <span className="transition-colors duration-300 group-hover:text-white">{children}</span>
      </span>
    </motion.button>
  );
}
