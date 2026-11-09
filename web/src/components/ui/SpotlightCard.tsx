"use client";

import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Glass surface with real depth: layered translucent fill, a luminous top edge, a soft drop
 * shadow, and a radial highlight that tracks the cursor (the "spotlight"). Replaces flat bordered
 * blocks so panels read as crafted glass rather than boxes. */
export function SpotlightCard({
  children,
  className,
  spot = "rgba(120,150,255,0.16)",
  interactive = true,
}: {
  children: ReactNode;
  className?: string;
  spot?: string;
  interactive?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - r.left}px`);
    el.style.setProperty("--my", `${e.clientY - r.top}px`);
  };

  return (
    <div
      ref={ref}
      onMouseMove={interactive ? onMove : undefined}
      className="group relative overflow-hidden rounded-[20px]"
      style={{
        background:
          "linear-gradient(180deg, rgba(255,255,255,0.05) 0%, rgba(255,255,255,0.012) 34%, rgba(6,7,10,0.35) 100%)",
        border: "1px solid rgba(255,255,255,0.07)",
        boxShadow:
          "inset 0 1px 0 0 rgba(255,255,255,0.08), inset 0 0 0 1px rgba(255,255,255,0.015), 0 34px 80px -46px rgba(0,0,0,0.95), 0 8px 24px -18px rgba(0,0,0,0.7)",
        backdropFilter: "blur(14px)",
        WebkitBackdropFilter: "blur(14px)",
      }}
    >
      {interactive && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          style={{
            background: `radial-gradient(340px circle at var(--mx, 50%) var(--my, 0%), ${spot}, transparent 68%)`,
          }}
        />
      )}
      <div className={cn("relative", className)}>{children}</div>
    </div>
  );
}
