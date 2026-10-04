"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/inbox", label: "Inbox" },
  { href: "/knowledge", label: "Knowledge" },
  { href: "/gaps", label: "Knowledge gaps" },
  { href: "/bookings", label: "Bookings & leads" },
  { href: "/settings", label: "Settings" },
];

export function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`) || (href === "/inbox" && pathname.startsWith("/traces"));
}

export function Sidebar({ business }: { business: string }) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  async function signOut() {
    await fetch("/api/session", { method: "DELETE" });
    router.replace("/login");
    router.refresh();
  }

  return (
    <aside className="bg-ink text-white lg:sticky lg:top-0 lg:flex lg:h-screen lg:w-64 lg:shrink-0 lg:flex-col">
      <div className="flex items-center justify-between gap-3 px-5 py-4 lg:py-7">
        <div className="flex min-w-0 items-center gap-3">
          <span aria-hidden="true" className="grid size-10 shrink-0 place-items-center rounded-2xl bg-accent font-black text-ink">
            {business.trim().charAt(0).toUpperCase() || "B"}
          </span>
          <div className="min-w-0">
            <p className="truncate font-semibold">{business}</p>
            <p className="text-xs text-white/70">Dashboard</p>
          </div>
        </div>
        <button
          type="button"
          className="rounded-pill bg-white/10 px-4 py-2 text-sm font-semibold lg:hidden"
          aria-expanded={open}
          aria-controls="main-nav"
          onClick={() => setOpen(!open)}
        >
          Menu
        </button>
      </div>
      <nav id="main-nav" aria-label="Main" className={`${open ? "block" : "hidden"} px-3 pb-4 lg:block lg:flex-1`}>
        <ul className="flex flex-col gap-1">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  onClick={() => setOpen(false)}
                  className={`block rounded-pill px-4 py-2.5 font-medium transition ${active ? "bg-accent text-ink" : "text-white/85 hover:bg-white/10 hover:text-white"}`}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className={`${open ? "block" : "hidden"} px-5 pb-6 lg:block`}>
        <button type="button" onClick={signOut} className="w-full rounded-pill bg-white/10 px-4 py-2.5 text-sm font-semibold hover:bg-white/15">
          Sign out
        </button>
      </div>
    </aside>
  );
}
