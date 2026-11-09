"use client";

import { useRef, useState } from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

const TIP_WIDTH = 224;
const TIP_GAP = 6;
const VIEWPORT_MARGIN = 8;

// Opaque surface so the popover stays readable over any content it floats above.
const TIP_SURFACE = {
  background: "rgba(16,18,26,0.98)",
  boxShadow:
    "0 10px 30px -10px rgba(0,0,0,0.65), inset 0 1px 0 rgba(255,255,255,0.06), 0 0 0 1px rgba(255,255,255,0.10)",
} as const;

interface TipPosition {
  top: number;
  left: number;
  flip: boolean;
}

/** A small "?" affordance that reveals an explanation on hover or keyboard focus. The popover is
 * rendered in a portal with fixed positioning, so `overflow-hidden` ancestors (the launch card)
 * can never clip it; it clamps to the viewport horizontally and flips upward near the bottom. */
export function InfoTip({
  text,
  label,
  className,
}: {
  text: string;
  label: string;
  className?: string;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [pos, setPos] = useState<TipPosition | null>(null);

  const show = () => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(
      Math.max(VIEWPORT_MARGIN, rect.left + rect.width / 2 - TIP_WIDTH / 2),
      window.innerWidth - TIP_WIDTH - VIEWPORT_MARGIN,
    );
    const flip = rect.bottom > window.innerHeight - 140;
    setPos({ top: flip ? rect.top - TIP_GAP : rect.bottom + TIP_GAP, left, flip });
  };
  const hide = () => setPos(null);

  return (
    <span className={cn("inline-flex align-middle", className)}>
      <button
        ref={trigger}
        type="button"
        aria-label={`${label}: more info`}
        onClick={(e) => e.preventDefault()}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        className="flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-semibold leading-none text-faint ring-1 ring-inset ring-white/15 transition-colors hover:text-foreground hover:ring-white/35 focus:outline-none focus-visible:text-foreground focus-visible:ring-accent"
      >
        ?
      </button>
      {pos &&
        createPortal(
          <span
            role="tooltip"
            style={{
              ...TIP_SURFACE,
              position: "fixed",
              top: pos.top,
              left: pos.left,
              width: TIP_WIDTH,
              transform: pos.flip ? "translateY(-100%)" : undefined,
            }}
            className="pointer-events-none z-[80] rounded-lg px-3 py-2 text-[11px] font-normal normal-case leading-relaxed tracking-normal text-muted"
          >
            {text}
          </span>,
          document.body,
        )}
    </span>
  );
}
