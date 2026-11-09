"use client";

import { BGPattern } from "@/components/ui/BgPattern";
import { ProgressiveFluxLoader } from "@/components/ui/ProgressiveFluxLoader";

const PHASES = [
  { at: 0, label: "connecting" },
  { at: 22, label: "checking balance" },
  { at: 46, label: "selecting markets" },
  { at: 70, label: "placing quotes" },
  { at: 90, label: "syncing dashboard" },
];

export function StartupLoader() {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden">
      <BGPattern variant="grid" mask="fade-edges" size={38} fill="rgba(170,182,200,0.16)" />
      <BGPattern variant="dots" mask="fade-top" size={26} fill="rgba(232,163,61,0.10)" />

      <div className="relative z-10 w-full max-w-md px-6 text-center">
        <div className="mb-10 inline-flex items-center gap-2 rounded-full border border-line bg-base/40 px-3 py-1">
          <span className="h-1.5 w-1.5 rounded-full bg-gold live-dot" />
          <span className="eyebrow">Launching reward farm</span>
        </div>
        <ProgressiveFluxLoader phases={PHASES} duration={16} loop />
        <p className="mt-8 text-[12.5px] text-faint">
          Bringing the bot online and syncing live data…
        </p>
      </div>
    </div>
  );
}
