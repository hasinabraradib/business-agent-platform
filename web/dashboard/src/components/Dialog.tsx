"use client";
/** A modal dialog on the native <dialog> element: focus moves in, Escape closes it, and the
 * page behind is inert. */
import { type ReactNode, useEffect, useRef } from "react";

export function Dialog({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal?.();
    if (!open && dialog.open) dialog.close?.();
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby="dialog-title"
      onClose={onClose}
      onCancel={onClose}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-panel bg-surface p-0 text-ink shadow-panel backdrop:bg-ink/40"
    >
      {open ? (
        <div className="flex flex-col gap-4 p-6">
          <h2 id="dialog-title" className="text-lg font-bold">
            {title}
          </h2>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
