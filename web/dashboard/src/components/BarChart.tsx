/** A simple, accessible bar chart: one idea per chart, values written out, no chart library. */
export interface Bar {
  label: string;
  value: number;
  tone?: "ink" | "accent" | "violet" | "danger" | "muted";
}

const FILL: Record<NonNullable<Bar["tone"]>, string> = {
  ink: "bg-ink",
  accent: "bg-accent",
  violet: "bg-violet",
  danger: "bg-danger",
  muted: "bg-subtle-hover",
};

export function BarChart({ title, bars, orientation = "horizontal", unit = "" }: { title: string; bars: Bar[]; orientation?: "horizontal" | "vertical"; unit?: string }) {
  const max = Math.max(1, ...bars.map((b) => b.value));
  if (orientation === "vertical") {
    return (
      <figure>
        <figcaption className="mb-4 font-semibold">{title}</figcaption>
        <ul className="flex h-44 items-end gap-2 sm:gap-3">
          {bars.map((bar) => (
            <li key={bar.label} className="flex h-full flex-1 flex-col items-center justify-end gap-1.5">
              <span className="text-xs font-semibold">{bar.value}</span>
              <span aria-hidden="true" className={`w-full max-w-12 rounded-t-xl ${FILL[bar.tone ?? "ink"]}`} style={{ height: `${Math.max(4, (bar.value / max) * 100)}%` }} />
              <span className="text-xs text-muted">
                <span className="sr-only">{`${bar.value}${unit} on `}</span>
                {bar.label}
              </span>
            </li>
          ))}
        </ul>
      </figure>
    );
  }
  return (
    <figure>
      <figcaption className="mb-4 font-semibold">{title}</figcaption>
      <ul className="flex flex-col gap-3">
        {bars.map((bar) => (
          <li key={bar.label} className="grid grid-cols-[7.5rem_1fr_3rem] items-center gap-3 text-sm">
            <span className="text-ink-soft">{bar.label}</span>
            <span aria-hidden="true" className="h-3 overflow-hidden rounded-pill bg-subtle">
              <span className={`block h-full rounded-pill ${FILL[bar.tone ?? "ink"]}`} style={{ width: `${(bar.value / max) * 100}%` }} />
            </span>
            <span className="text-right font-semibold">
              {bar.value}
              {unit}
            </span>
          </li>
        ))}
      </ul>
    </figure>
  );
}
