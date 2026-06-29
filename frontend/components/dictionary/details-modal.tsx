"use client";

import { ItemDetailModal } from "@/components/items/item-detail";
import type { GroupedItem } from "@/components/dictionary/grouping";

/**
 * Data Dictionary "Details" popup — a thin wrapper over the shared
 * <ItemDetailModal/>. The grouped representative plus its instances drive the
 * governance chips, the instances table and the set-primary action.
 */
export function DetailsModal({
  group,
  onClose,
  onSetPrimary,
}: {
  group: GroupedItem | null;
  onClose: () => void;
  onSetPrimary: (itemId: string) => void;
}) {
  return (
    <ItemDetailModal
      open={!!group}
      onClose={onClose}
      item={group}
      instances={group?._instances}
      onSetPrimary={onSetPrimary}
    />
  );
}
