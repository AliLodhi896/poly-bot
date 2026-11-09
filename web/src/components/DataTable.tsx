"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import type { Row } from "@/lib/types";

export interface Column {
  key: string;
  header: string;
  align?: "left" | "right";
  width?: string;
  render: (row: Row) => ReactNode;
  className?: string;
}

interface DataTableProps {
  columns: Column[];
  rows: Row[];
  rowKey: (row: Row, i: number) => string;
  empty?: string;
  maxHeight?: number;
}

export function DataTable({
  columns,
  rows,
  rowKey,
  empty = "No records yet",
  maxHeight = 320,
}: DataTableProps) {
  // Briefly flash rows that appeared since the previous render. The "fresh" set is
  // kept in state (computed in an effect) so we never read a ref during render.
  const seen = useRef<Set<string>>(new Set());
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const keys = rows.map((r, i) => rowKey(r, i));
  const sig = keys.join("|");

  useEffect(() => {
    const next = new Set<string>();
    const justNew = new Set<string>();
    for (const k of keys) {
      next.add(k);
      if (!seen.current.has(k)) justNew.add(k);
    }
    setFresh(seen.current.size === 0 ? new Set() : justNew);
    seen.current = next;
    // keys is derived from sig; depending on sig keeps this to real row-set changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  if (rows.length === 0) {
    return (
      <div className="flex items-center gap-2 px-4 py-8 text-[13px] text-faint">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-line-strong" />
        {empty}
      </div>
    );
  }

  return (
    <div className="scroll-thin overflow-auto" style={{ maxHeight }}>
      <table className="w-full border-collapse text-[12.5px]">
        <thead className="sticky top-0 z-10 bg-panel/95 backdrop-blur-sm">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ width: c.width }}
                className={cn(
                  "border-b border-line px-3 py-2.5 text-[10px] font-medium uppercase tracking-wide text-faint",
                  c.align === "right" ? "text-right" : "text-left",
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const k = keys[i];
            return (
              <tr
                key={k}
                className={cn(
                  "border-b border-line/60 transition-colors hover:bg-panel-2/75",
                  fresh.has(k) && "row-flash",
                )}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={cn(
                      "px-3 py-2.5 tnum text-foreground/90",
                      c.align === "right" ? "text-right" : "text-left",
                      c.className,
                    )}
                  >
                    {c.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
