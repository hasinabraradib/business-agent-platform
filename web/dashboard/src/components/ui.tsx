/** Small building blocks in the house style: pill buttons, cards, badges and states. */
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Tone = "primary" | "accent" | "secondary" | "danger" | "ghost";

const TONES: Record<Tone, string> = {
  primary: "bg-ink text-white hover:bg-ink-soft",
  accent: "bg-accent text-ink hover:brightness-95",
  secondary: "bg-subtle text-ink hover:bg-subtle-hover",
  danger: "bg-danger-soft text-danger hover:bg-[#fde4e1]",
  ghost: "bg-transparent text-ink hover:bg-subtle",
};

export function Button({
  tone = "primary",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone; size?: "sm" | "md" }) {
  const sizes = size === "sm" ? "px-3.5 py-1.5 text-sm" : "px-5 py-2.5 text-[15px]";
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center justify-center gap-2 rounded-pill font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${sizes} ${TONES[tone]} ${className}`}
    />
  );
}

export function Card({ children, className = "", as: Tag = "section", ...rest }: { children: ReactNode; className?: string; as?: "section" | "div" | "article"; "aria-labelledby"?: string; "aria-label"?: string }) {
  return (
    <Tag {...rest} className={`rounded-panel bg-surface p-6 shadow-soft ${className}`}>
      {children}
    </Tag>
  );
}

const BADGE: Record<string, string> = {
  neutral: "bg-subtle text-ink-soft",
  accent: "bg-accent text-ink",
  violet: "bg-violet-soft text-violet-ink",
  danger: "bg-danger-soft text-danger",
  ink: "bg-ink text-white",
};

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: keyof typeof BADGE }) {
  return <span className={`inline-flex items-center gap-1 rounded-pill px-2.5 py-0.5 text-xs font-semibold ${BADGE[tone]}`}>{children}</span>;
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">{title}</h1>
        {description ? <p className="mt-1 max-w-2xl text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </header>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-3 py-6">
      <span className="sr-only">{label}…</span>
      {[0, 1, 2].map((i) => (
        <div key={i} aria-hidden="true" className="h-12 animate-pulse rounded-2xl bg-subtle" />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-panel border border-dashed border-subtle-hover px-6 py-12 text-center">
      <p className="text-lg font-semibold">{title}</p>
      {children ? <div className="max-w-md text-muted">{children}</div> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-danger-soft px-5 py-4 text-danger">
      <p className="font-medium">{message}</p>
      {onRetry ? (
        <Button tone="danger" size="sm" onClick={onRetry} className="bg-white">
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function Field({ label, hint, children, id }: { label: string; hint?: string; children: ReactNode; id: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-semibold">
        {label}
      </label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export const inputClass =
  "w-full rounded-2xl border border-subtle-hover bg-surface px-4 py-2.5 text-[15px] placeholder:text-muted focus-visible:border-focus";
