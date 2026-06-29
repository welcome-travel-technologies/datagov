"use client";

import { PageHeader } from "@/components/page-header";
import { DictionaryView } from "@/components/dictionary/dictionary-view";

export default function DictionaryPage() {
  return (
    <div>
      <PageHeader
        title="Data Dictionary"
        description="Search, govern, and document every catalog item across PowerBI and dbt."
      />
      <DictionaryView />
    </div>
  );
}
