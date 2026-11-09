"use client";

import * as React from "react";
import * as SliderPrimitive from "@radix-ui/react-slider";
import { cn } from "@/lib/utils";
import { InfoTip } from "@/components/ui/InfoTip";

// Radix-based sliders in the originui style, themed to gold-on-midnight.
const Base = React.forwardRef<
  React.ElementRef<typeof SliderPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root>
>(({ className, ...props }, ref) => {
  const thumbs = Array.isArray(props.value)
    ? props.value
    : Array.isArray(props.defaultValue)
      ? props.defaultValue
      : [0];
  return (
    <SliderPrimitive.Root
      ref={ref}
      className={cn("relative flex w-full touch-none select-none items-center", className)}
      {...props}
    >
      <SliderPrimitive.Track className="relative h-1 w-full grow overflow-hidden rounded-full bg-line">
        <SliderPrimitive.Range className="absolute h-full bg-gold" />
      </SliderPrimitive.Track>
      {thumbs.map((_, i) => (
        <SliderPrimitive.Thumb
          key={i}
          className="block h-4 w-4 cursor-grab rounded-full border border-gold bg-base shadow-[0_0_0_3px_rgba(232,163,61,0.15)] outline-none transition-[box-shadow] focus-visible:shadow-[0_0_0_5px_rgba(232,163,61,0.3)] active:cursor-grabbing"
        />
      ))}
    </SliderPrimitive.Root>
  );
});
Base.displayName = "SliderBase";

export { Base as SliderBase };

interface SliderWithOutputProps {
  label: string;
  value: number;
  onValueChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  format?: (n: number) => string;
  info?: string;
}

export function SliderWithOutput({
  label,
  value,
  onValueChange,
  min,
  max,
  step = 1,
  format = (n) => String(n),
  info,
}: SliderWithOutputProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <span className="flex items-center gap-1.5">
          <label className="eyebrow">{label}</label>
          {info && <InfoTip label={label} text={info} />}
        </span>
        <span className="tnum text-[15px] font-medium text-foreground">{format(value)}</span>
      </div>
      <Base
        value={[value]}
        min={min}
        max={max}
        step={step}
        onValueChange={(v) => onValueChange(v[0])}
        aria-label={label}
      />
    </div>
  );
}

interface RangeSliderProps {
  label: string;
  value: [number, number];
  onValueChange: (value: [number, number]) => void;
  min: number;
  max: number;
  step?: number;
  format?: (n: number) => string;
}

export function RangeSlider({
  label,
  value,
  onValueChange,
  min,
  max,
  step = 1,
  format = (n) => String(n),
}: RangeSliderProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <label className="eyebrow">{label}</label>
        <span className="tnum text-[13px] font-medium text-foreground">
          {format(value[0])} <span className="text-faint">—</span> {format(value[1])}
        </span>
      </div>
      <Base
        value={value}
        min={min}
        max={max}
        step={step}
        minStepsBetweenThumbs={1}
        onValueChange={(v) => onValueChange([v[0], v[1]])}
        aria-label={label}
      />
    </div>
  );
}
