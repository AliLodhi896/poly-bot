"use client";

import { motion } from "motion/react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** A word (or phrase) with an animated highlighter sweep behind it — the marker wipes in from the
 * left on mount, and the text itself rises into place. Neutral text colour; the highlight carries
 * the accent, so no coloured type. */
export function Highlight({
  children,
  className,
  delay = 0.45,
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
}) {
  return (
    <span className={cn("relative inline-block", className)}>
      <motion.span
        aria-hidden
        className="absolute inset-x-[-0.12em] bottom-[0.06em] top-[0.08em] -z-0 origin-left rounded-[8px]"
        initial={{ scaleX: 0, opacity: 0 }}
        animate={{ scaleX: 1, opacity: 1 }}
        transition={{ delay, duration: 0.72, ease: [0.16, 1, 0.3, 1] }}
        style={{
          background:
            "linear-gradient(100deg, rgba(91,140,255,0.34) 0%, rgba(157,139,255,0.30) 55%, rgba(95,208,230,0.24) 100%)",
          boxShadow: "0 0 30px rgba(91,140,255,0.22)",
        }}
      />
      <motion.span
        className="relative z-10 inline-block"
        initial={{ opacity: 0, y: "0.32em", filter: "blur(6px)" }}
        animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
        transition={{ delay: delay - 0.2, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
      >
        {children}
      </motion.span>
    </span>
  );
}

/** A plain word that rises + unblurs into place — used for the non-highlighted words of a heading
 * so the whole line animates as one crafted unit. */
export function RiseWord({
  children,
  delay = 0,
  className,
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  return (
    <motion.span
      className={cn("inline-block", className)}
      initial={{ opacity: 0, y: "0.32em", filter: "blur(6px)" }}
      animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
      transition={{ delay, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.span>
  );
}
