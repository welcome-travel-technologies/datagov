"use client";

import { ItemDetailModal, useItemDetail } from "@/components/items/item-detail";

/** Pull the "TYPE::hash" hash out of a composite node id (mirrors showNodeModal). */
function idHashOf(nodeId: string): string {
  const i = nodeId.indexOf("::");
  return i >= 0 ? nodeId.slice(i + 2) : nodeId;
}

/**
 * Lineage / catalog node-click detail popup — a thin wrapper over the shared
 * <ItemDetailModal/>. Resolves the catalog `Item` behind a composite node id
 * (id-hash endpoint, name-search fallback) and renders the unified detail body.
 */
export function NodeDetailPanel({
  nodeId,
  label,
  onClose,
}: {
  nodeId: string | null;
  label: string;
  onClose: () => void;
}) {
  const { item, loading, notFound } = useItemDetail(nodeId ? idHashOf(nodeId) : null, label);

  return (
    <ItemDetailModal
      open={!!nodeId}
      onClose={onClose}
      item={item}
      loading={loading}
      notFound={notFound}
      title={label}
    />
  );
}
