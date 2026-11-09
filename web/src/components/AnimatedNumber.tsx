"use client";

import { useEffect } from "react";
import { useMotionValue, useSpring, useTransform, motion } from "motion/react";

interface AnimatedNumberProps {
  value: number;
  format: (n: number) => string;
}

// Springs the displayed figure toward the latest value so KPI cards tick up/down
// smoothly instead of snapping on every 2.5s poll.
export function AnimatedNumber({ value, format }: AnimatedNumberProps) {
  const motionValue = useMotionValue(value);
  const spring = useSpring(motionValue, { stiffness: 90, damping: 20, mass: 0.6 });
  const text = useTransform(spring, (n) => format(n));

  useEffect(() => {
    motionValue.set(value);
  }, [value, motionValue]);

  return <motion.span>{text}</motion.span>;
}
