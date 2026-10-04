import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { GapsClient } from "./GapsClient";

export const metadata: Metadata = { title: "Knowledge gaps" };

export default function GapsPage() {
  return (
    <>
      <PageHeader title="Knowledge gaps" description="Questions your assistant couldn't answer, grouped when they are alike. Add an answer and the assistant can use it from then on." />
      <GapsClient />
    </>
  );
}
