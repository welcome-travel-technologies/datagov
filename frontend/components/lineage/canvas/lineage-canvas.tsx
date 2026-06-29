"use client";

import { useEffect } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
} from "@xyflow/react";
import { nodeTypes } from "@/components/lineage/canvas/nodes";
import { edgeTypes } from "@/components/lineage/canvas/edges";
import type { XY } from "@/lib/lineage/layout";

export interface LineageCanvasProps {
  nodes: Node[];
  edges: Edge[];
  /** Bumped when the structure (not just styling) changes, to trigger a fit. */
  fitKey: string;
  onNodeMove: (id: string, pos: XY) => void;
  onPaneClick: () => void;
  miniMapColor: (n: Node) => string;
}

export function LineageCanvas({
  nodes,
  edges,
  fitKey,
  onNodeMove,
  onPaneClick,
  miniMapColor,
}: LineageCanvasProps) {
  const { fitView } = useReactFlow();
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    setRfNodes(nodes);
  }, [nodes, setRfNodes]);

  useEffect(() => {
    setRfEdges(edges);
  }, [edges, setRfEdges]);

  // Fit after a structural rebuild (new center / expand / collapse / arrange).
  // Clamp the auto-fit with a legibility floor: a full PowerBI measure lineage
  // is a long L→R chain whose true fit zoom is tiny, so fitting it all shrinks
  // the cards until they can't be read. Land no smaller than `minZoom` (cards
  // stay legible) and pan to reach the ends; the toolbar's "Fit to view" still
  // zooms all the way out to show everything at once.
  useEffect(() => {
    if (nodes.length === 0) return;
    const t = setTimeout(() => fitView({ padding: 0.12, duration: 300, minZoom: 0.4 }), 60);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey, fitView]);

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodeDragStop={(_e, node) => onNodeMove(node.id, node.position)}
      onPaneClick={onPaneClick}
      minZoom={0.1}
      maxZoom={2.5}
      proOptions={{ hideAttribution: true }}
      fitView
    >
      <Background gap={20} color="oklch(0.92 0.004 90)" />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable nodeColor={miniMapColor} nodeStrokeWidth={2} maskColor="oklch(0.97 0.003 95 / 0.6)" />
    </ReactFlow>
  );
}
