import { cn } from "@/lib/cn";
import { num } from "@/lib/format";

/** Inline value tinted by sign — for PnL-style numbers in tables. */
export function Pnl({ value, render }: { value: unknown; render: (v: unknown) => string }) {
  const n = num(value);
  const tone = n === null || n === 0 ? "text-muted" : n > 0 ? "text-profit" : "text-loss";
  return <span className={cn("tnum", tone)}>{render(value)}</span>;
}

export function Side({ side }: { side: unknown }) {
  const s = String(side ?? "").toUpperCase();
  if (s === "BUY")
    return (
      <span className="rounded bg-profit/12 px-1.5 py-0.5 text-[11px] font-semibold text-profit">
        BUY
      </span>
    );
  if (s === "SELL")
    return (
      <span className="rounded bg-loss/12 px-1.5 py-0.5 text-[11px] font-semibold text-loss">
        SELL
      </span>
    );
  return <span className="text-muted">{s || "—"}</span>;
}

export function Yes({ on }: { on: unknown }) {
  const v = Number(on || 0);
  return v ? (
    <span className="text-profit">●</span>
  ) : (
    <span className="text-faint">○</span>
  );
}
