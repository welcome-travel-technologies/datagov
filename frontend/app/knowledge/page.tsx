import { redirect } from "next/navigation";

/** Preserve old bookmarks while content management lives in Integrations. */
export default function KnowledgePage() { redirect("/integrations?tab=content"); }
