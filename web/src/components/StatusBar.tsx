"use client";

import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { cn } from "@/lib/cn";
import type { ConnState } from "@/lib/useSnapshot";
import type { RewardRun } from "@/lib/types";
import { dur } from "@/lib/format";

interface StatusBarProps {
  run: RewardRun | null;
  conn: ConnState;
  /** The trading mode of the running bot — the pill must reflect it, not data availability. */
  mode: "paper" | "live";
}

function RunPill({ run, conn, mode }: { run: RewardRun | null; conn: ConnState; mode: "paper" | "live" }) {
  const isRunning = run?.status === "running" && conn === "online";
  const isLive = isRunning && mode === "live";
  if (!run) {
    const label =
      conn === "offline" ? "API OFFLINE" : conn === "connecting" ? "CONNECTING" : "Idle";
    return (
      <span
        className={cn(
          "flex items-center gap-2 rounded-md border px-2.5 py-1 text-[12px] font-medium tracking-wide",
          conn === "offline"
            ? "border-loss/30 bg-loss/10 text-loss"
            : conn === "connecting"
              ? "border-warn/30 bg-warn/10 text-warn"
              : "border-line bg-surface text-muted",
        )}
      >
        <span
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            conn === "offline" ? "bg-loss" : conn === "connecting" ? "bg-warn" : "bg-faint",
          )}
        />
        {label}
      </span>
    );
  }
  return (
    <span
      className={cn(
        "flex items-center gap-2 rounded-md border px-2.5 py-1 text-[12px] font-medium tracking-wide",
        isLive
          ? "border-profit/30 bg-profit/10 text-profit"
          : "border-warn/30 bg-warn/10 text-warn",
      )}
    >
      <span
        className={cn("h-1.5 w-1.5 rounded-full", isLive ? "bg-profit live-dot" : "bg-warn")}
      />
      {isRunning ? (isLive ? "LIVE" : "PAPER") : run.status.toUpperCase()}
    </span>
  );
}

export function StatusBar({ run, conn, mode }: StatusBarProps) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    const update = () => setNow(Date.now());
    const first = setTimeout(update, 0);
    const id = setInterval(update, 1000);
    return () => {
      clearTimeout(first);
      clearInterval(id);
    };
  }, []);

  const uptime = now === null || !run ? null : dur(Math.max(0, now / 1000 - run.started_at));

  return (
    <motion.header
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: "easeOut" }}
      className="sticky top-0 z-30 border-b border-line/70 bg-base/60 backdrop-blur-md"
    >
      <div className="mx-auto flex max-w-[1440px] items-center justify-end gap-4 px-3 py-3 sm:px-5 lg:px-6">
        {run && uptime && (
          <span className="text-[12px] text-faint">
            up <span className="tnum text-muted">{uptime}</span>
          </span>
        )}
        <RunPill run={run} conn={conn} mode={mode} />
      </div>
    </motion.header>
  );
}
