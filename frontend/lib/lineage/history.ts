/**
 * Lineage undo/redo: a thin re-export of the shared generic history stack
 * (`@/lib/history`). The lineage store passes a serializable slice of its state
 * (Sets flattened to arrays) as the snapshot type.
 */
export { useHistory, type History } from "@/lib/history";
