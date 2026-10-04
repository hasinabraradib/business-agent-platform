"use client";
/** Run a callback every N ms, only while the page is visible (and once when it comes back). */
import { useEffect, useRef } from "react";

export function usePolling(callback: () => void | Promise<void>, intervalMs: number, enabled = true): void {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback; // always call the latest callback without restarting the timer
  }, [callback]);
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer === null) timer = setInterval(() => void saved.current(), intervalMs);
    };
    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void saved.current();
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, enabled]);
}
