import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { api } from "@/lib/server";
import type { Lead, Reservation } from "@/lib/types";
import { LeadsTable, ReservationsTable } from "./Tables";

export const metadata: Metadata = { title: "Bookings & leads" };
export const dynamic = "force-dynamic";

export default async function BookingsPage() {
  const [reservations, leads] = await Promise.all([api<Reservation[]>("/reservations"), api<Lead[]>("/leads")]);
  return (
    <>
      <PageHeader title="Bookings & leads" description="Table bookings and sales enquiries the assistant took, each linked to its conversation." />
      <div className="flex flex-col gap-8">
        <ReservationsTable rows={reservations} />
        <LeadsTable rows={leads} />
      </div>
    </>
  );
}
