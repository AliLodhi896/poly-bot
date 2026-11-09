"use client";

import { DottedSurface } from "./DottedSurface";

export function AmbientVisuals() {
  return (
    <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden" aria-hidden>
      <div className="absolute inset-0 bg-[linear-gradient(180deg,rgba(255,255,255,0.026)_0%,rgba(255,255,255,0)_18%),linear-gradient(90deg,rgba(255,255,255,0.022)_1px,transparent_1px),linear-gradient(180deg,rgba(255,255,255,0.018)_1px,transparent_1px)] bg-[size:100%_100%,72px_72px,72px_72px]" />

      <div className="absolute inset-x-0 bottom-[-9vh] h-[62vh] min-h-[380px] opacity-90">
        <DottedSurface className="absolute inset-0" />
        <div className="absolute inset-0 bg-gradient-to-b from-base/[0.84] via-base/[0.08] to-base/[0.28]" />
      </div>
      <div className="absolute inset-x-0 bottom-0 h-[34vh] bg-gradient-to-t from-base via-base/[0.24] to-transparent" />
    </div>
  );
}
