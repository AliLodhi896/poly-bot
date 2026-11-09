import React from "react";
import { type VariantProps, cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Adapted from 21st.dev cybergaz/neon-button, themed to the gold-on-midnight palette.
const buttonVariants = cva(
  "relative group cursor-pointer border text-center rounded-full transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-50 text-[13px] font-medium tracking-wide",
  {
    variants: {
      variant: {
        default: "bg-gold/5 hover:bg-gold/10 border-gold/25 text-foreground",
        solid: "bg-gold text-[#0b0f18] border-transparent hover:bg-gold-soft",
        ghost: "border-line bg-transparent text-muted hover:border-line-strong hover:bg-foreground/5 hover:text-foreground",
      },
      size: {
        default: "px-6 py-1.5",
        sm: "px-4 py-1",
        lg: "px-9 py-2.5",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface NeonButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  neon?: boolean;
}

export const NeonButton = React.forwardRef<HTMLButtonElement, NeonButtonProps>(
  ({ className, neon = true, size, variant, children, ...props }, ref) => {
    return (
      <button className={cn(buttonVariants({ variant, size }), className)} ref={ref} {...props}>
        <span
          className={cn(
            "absolute h-px opacity-0 group-hover:opacity-100 transition-all duration-500 ease-in-out inset-x-0 top-0 bg-gradient-to-r w-3/4 mx-auto from-transparent via-gold to-transparent hidden",
            neon && "block",
          )}
        />
        {children}
        <span
          className={cn(
            "absolute opacity-0 group-hover:opacity-40 transition-all duration-500 ease-in-out inset-x-0 h-px -bottom-px bg-gradient-to-r w-3/4 mx-auto from-transparent via-gold to-transparent hidden",
            neon && "block",
          )}
        />
      </button>
    );
  },
);

NeonButton.displayName = "NeonButton";
export { buttonVariants };
