import { str } from "@/lib/format";

export function QuestionCell({ q }: { q: unknown }) {
  const text = str(q);
  return (
    <span className="block max-w-[340px] truncate text-foreground/90" title={text}>
      {text}
    </span>
  );
}
