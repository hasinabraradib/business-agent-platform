import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { KnowledgeClient } from "./KnowledgeClient";

export const metadata: Metadata = { title: "Knowledge" };

export default function KnowledgePage() {
  return (
    <>
      <PageHeader title="Knowledge" description="What your assistant knows: menus, product lists, policies and web pages. It only answers from these." />
      <KnowledgeClient />
    </>
  );
}
