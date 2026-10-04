import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 px-4 text-center">
      <h1 className="text-2xl font-bold">Page not found</h1>
      <p className="text-muted">That page doesn&apos;t exist, or the item was removed.</p>
      <Link href="/" className="rounded-pill bg-ink px-5 py-2.5 font-semibold text-white">
        Back to the overview
      </Link>
    </main>
  );
}
