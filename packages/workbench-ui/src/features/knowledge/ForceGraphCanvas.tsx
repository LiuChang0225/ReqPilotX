import { useEffect, useRef, useState, useCallback } from "react";
import type { KnowledgeNode, KnowledgeLink } from "./knowledgeData";

interface ForceGraphCanvasProps {
  nodes: KnowledgeNode[];
  links: KnowledgeLink[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string) => void;
  filterCategory?: string | null;
  searchKeyword?: string;
  highlightedPath?: string[];
  isLocalGraph?: boolean;
}

interface SimNode extends KnowledgeNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
}

export function findShortestPath(
  links: KnowledgeLink[],
  startId: string,
  endId: string
): string[] {
  if (!startId || !endId) return [];
  if (startId === endId) return [startId];

  const adj = new Map<string, string[]>();
  for (const l of links) {
    if (!adj.has(l.source)) adj.set(l.source, []);
    if (!adj.has(l.target)) adj.set(l.target, []);
    adj.get(l.source)!.push(l.target);
    adj.get(l.target)!.push(l.source);
  }

  const queue: string[][] = [[startId]];
  const visited = new Set<string>([startId]);

  while (queue.length > 0) {
    const path = queue.shift()!;
    const last = path[path.length - 1];

    if (last === endId) return path;

    const neighbors = adj.get(last) || [];
    for (const n of neighbors) {
      if (!visited.has(n)) {
        visited.add(n);
        queue.push([...path, n]);
      }
    }
  }

  return [];
}

export function ForceGraphCanvas({
  nodes,
  links,
  selectedNodeId,
  onSelectNode,
  filterCategory,
  searchKeyword,
  highlightedPath = [],
  isLocalGraph = false,
}: ForceGraphCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const simNodesRef = useRef<SimNode[]>([]);
  const transformRef = useRef({ x: 0, y: 0, k: isLocalGraph ? 0.95 : 1 });
  const isDraggingCanvasRef = useRef(false);
  const dragStartRef = useRef({ x: 0, y: 0 });
  const draggedNodeRef = useRef<SimNode | null>(null);
  const hoveredNodeRef = useRef<SimNode | null>(null);
  const animFrameIdRef = useRef<number | null>(null);

  const [hoveredNodeName, setHoveredNodeName] = useState<string | null>(null);

  // Initialize nodes
  useEffect(() => {
    const width = containerRef.current?.clientWidth || (isLocalGraph ? 320 : 800);
    const height = containerRef.current?.clientHeight || (isLocalGraph ? 210 : 600);

    const existingMap = new Map<string, SimNode>();
    for (const n of simNodesRef.current) {
      existingMap.set(n.id, n);
    }

    const cx = width / 2;
    const cy = height / 2;

    simNodesRef.current = nodes.map((node, i) => {
      const existing = existingMap.get(node.id);
      if (existing) {
        return {
          ...node,
          x: existing.x,
          y: existing.y,
          vx: existing.vx,
          vy: existing.vy,
          radius: isLocalGraph
            ? node.id === selectedNodeId
              ? 11
              : 7
            : node.val === 15
            ? 12
            : 8,
        };
      }

      // Constellation Cluster initialization
      let targetX = cx;
      let targetY = cy;

      if (!isLocalGraph) {
        if (node.category === "specs") {
          targetX = cx - 180 + (Math.random() - 0.5) * 80;
          targetY = cy - 120 + (Math.random() - 0.5) * 80;
        } else if (node.category === "dvp_tests") {
          targetX = cx + 180 + (Math.random() - 0.5) * 80;
          targetY = cy - 120 + (Math.random() - 0.5) * 80;
        } else if (node.category === "interfaces") {
          targetX = cx - 160 + (Math.random() - 0.5) * 80;
          targetY = cy + 130 + (Math.random() - 0.5) * 80;
        } else if (node.category === "integration_rules") {
          targetX = cx + 160 + (Math.random() - 0.5) * 80;
          targetY = cy + 130 + (Math.random() - 0.5) * 80;
        }
      } else {
        const angle = (2 * Math.PI * i) / (nodes.length || 1);
        const r = node.id === selectedNodeId ? 0 : 70;
        targetX = cx + r * Math.cos(angle);
        targetY = cy + r * Math.sin(angle);
      }

      return {
        ...node,
        x: targetX,
        y: targetY,
        vx: 0,
        vy: 0,
        radius: isLocalGraph
          ? node.id === selectedNodeId
            ? 11
            : 7
          : node.val === 15
          ? 12
          : 8,
      };
    });
  }, [nodes, isLocalGraph, selectedNodeId]);

  // Main Render & Force Simulation
  useEffect(() => {
    let isMounted = true;

    const render = () => {
      if (!isMounted) return;

      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      if (!canvas || !ctx) {
        animFrameIdRef.current = requestAnimationFrame(render);
        return;
      }

      const width = canvas.width;
      const height = canvas.height;
      const cx = width / 2;
      const cy = height / 2;
      const simNodes = simNodesRef.current;

      // 1. Force Simulation Step
      const kRepulsion = isLocalGraph ? 800 : 1200;
      const kSpring = isLocalGraph ? 0.06 : 0.04;
      const springLength = isLocalGraph ? 70 : 100;
      const damping = 0.86;

      const nodeMap = new Map<string, SimNode>();
      for (const n of simNodes) nodeMap.set(n.id, n);

      // Repulsion
      for (let i = 0; i < simNodes.length; i++) {
        for (let j = i + 1; j < simNodes.length; j++) {
          const n1 = simNodes[i];
          const n2 = simNodes[j];
          const dx = n2.x - n1.x;
          const dy = n2.y - n1.y;
          const distSq = dx * dx + dy * dy || 1;
          const dist = Math.sqrt(distSq);

          if (dist < 260) {
            const force = kRepulsion / distSq;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            n1.vx -= fx;
            n1.vy -= fy;
            n2.vx += fx;
            n2.vy += fy;
          }
        }
      }

      // Spring tension
      for (const link of links) {
        const s = nodeMap.get(link.source);
        const t = nodeMap.get(link.target);
        if (!s || !t) continue;

        const dx = t.x - s.x;
        const dy = t.y - s.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const displacement = dist - springLength;
        const force = displacement * kSpring;

        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        s.vx += fx;
        s.vy += fy;
        t.vx -= fx;
        t.vy -= fy;
      }

      // Constellation Cluster Center Gravity
      for (const n of simNodes) {
        if (draggedNodeRef.current?.id === n.id) continue;

        let anchorX = cx;
        let anchorY = cy;

        if (!isLocalGraph) {
          if (n.category === "specs") {
            anchorX = cx - 210;
            anchorY = cy - 100;
          } else if (n.category === "ee_hardware") {
            anchorX = cx - 70;
            anchorY = cy - 190;
          } else if (n.category === "software_diagnostics") {
            anchorX = cx + 80;
            anchorY = cy - 190;
          } else if (n.category === "dvp_tests") {
            anchorX = cx + 210;
            anchorY = cy - 100;
          } else if (n.category === "interfaces") {
            anchorX = cx - 160;
            anchorY = cy + 130;
          } else if (n.category === "integration_rules") {
            anchorX = cx + 160;
            anchorY = cy + 130;
          }
        } else {
          if (n.id === selectedNodeId) {
            anchorX = cx;
            anchorY = cy;
          }
        }

        const clusterPull = isLocalGraph && n.id === selectedNodeId ? 0.08 : 0.025;
        n.vx += (anchorX - n.x) * clusterPull;
        n.vy += (anchorY - n.y) * clusterPull;

        n.vx *= damping;
        n.vy *= damping;

        n.x += n.vx;
        n.y += n.vy;
      }

      // 2. Clear & Draw Canvas
      ctx.clearRect(0, 0, width, height);

      ctx.save();
      const { x: tx, y: ty, k: tk } = transformRef.current;
      ctx.translate(tx, ty);
      ctx.scale(tk, tk);

      // Elegant Grid
      if (!isLocalGraph) {
        ctx.lineWidth = 0.5 / tk;
        ctx.strokeStyle = "rgba(148, 163, 184, 0.08)";
        const gridSize = 45;
        const startX = Math.floor(-tx / tk / gridSize) * gridSize;
        const endX = startX + (width / tk) + gridSize * 2;
        const startY = Math.floor(-ty / tk / gridSize) * gridSize;
        const endY = startY + (height / tk) + gridSize * 2;

        ctx.beginPath();
        for (let gx = startX; gx <= endX; gx += gridSize) {
          ctx.moveTo(gx, startY);
          ctx.lineTo(gx, endY);
        }
        for (let gy = startY; gy <= endY; gy += gridSize) {
          ctx.moveTo(startX, gy);
          ctx.lineTo(endX, gy);
        }
        ctx.stroke();
      }

      // Hover / Spotlight
      const activeHover = hoveredNodeRef.current;
      const targetFocus = activeHover || (selectedNodeId ? nodeMap.get(selectedNodeId) : null);

      const highlightedIds = new Set<string>();
      if (targetFocus) {
        highlightedIds.add(targetFocus.id);
        for (const l of links) {
          if (l.source === targetFocus.id) highlightedIds.add(l.target);
          if (l.target === targetFocus.id) highlightedIds.add(l.source);
        }
      }

      const pathSet = new Set<string>(highlightedPath);
      const isPathActive = highlightedPath.length > 1;

      // Draw Links
      for (const link of links) {
        const s = nodeMap.get(link.source);
        const t = nodeMap.get(link.target);
        if (!s || !t) continue;

        const isConnected =
          targetFocus &&
          (link.source === targetFocus.id || link.target === targetFocus.id);

        const isPathEdge =
          isPathActive &&
          pathSet.has(link.source) &&
          pathSet.has(link.target) &&
          Math.abs(highlightedPath.indexOf(link.source) - highlightedPath.indexOf(link.target)) === 1;

        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(t.x, t.y);

        if (isPathEdge) {
          ctx.strokeStyle = "#f59e0b"; // Gold beam
          ctx.lineWidth = 3.0 / tk;
        } else if (isConnected) {
          ctx.strokeStyle = "rgba(0, 137, 61, 0.85)"; // Brand green connection
          ctx.lineWidth = 2.0 / tk;
        } else if (isPathActive || (targetFocus && !isLocalGraph)) {
          ctx.strokeStyle = "rgba(148, 163, 184, 0.12)";
          ctx.lineWidth = 0.8 / tk;
        } else {
          ctx.strokeStyle = "rgba(148, 163, 184, 0.32)";
          ctx.lineWidth = 1.0 / tk;
        }
        ctx.stroke();
      }

      // Draw Nodes
      for (const n of simNodes) {
        const isSelected = n.id === selectedNodeId;
        const isHovered = activeHover?.id === n.id;
        const isHighlighted = highlightedIds.has(n.id);
        const isInPath = pathSet.has(n.id);
        const pathIndex = highlightedPath.indexOf(n.id);
        const matchesSearch =
          searchKeyword &&
          (n.name.toLowerCase().includes(searchKeyword.toLowerCase()) ||
            n.tags.some((t) => t.toLowerCase().includes(searchKeyword.toLowerCase())));

        let alpha = 1.0;
        if (isPathActive && !isInPath) alpha = 0.2;
        else if (targetFocus && !isHighlighted && !isInPath && !isLocalGraph) alpha = 0.25;

        // Outer Aura
        if (isInPath || isSelected || isHovered || matchesSearch) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.radius + (isInPath ? 7 : 5), 0, 2 * Math.PI);
          ctx.fillStyle = isInPath
            ? "rgba(245, 158, 11, 0.35)"
            : isSelected
            ? "rgba(0, 137, 61, 0.35)"
            : "rgba(148, 163, 184, 0.3)";
          ctx.fill();
        }

        // Inner Disc
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius, 0, 2 * Math.PI);
        ctx.globalAlpha = alpha;
        ctx.fillStyle = isInPath ? "#f59e0b" : isSelected ? "#00893d" : n.color || "#2563eb";
        ctx.fill();

        ctx.lineWidth = isSelected || isInPath ? 2.2 : 1.0;
        ctx.strokeStyle = isSelected || isInPath ? "#ffffff" : "rgba(255, 255, 255, 0.7)";
        ctx.stroke();
        ctx.globalAlpha = 1.0;

        // Path Index Badge
        if (isInPath && pathIndex >= 0) {
          ctx.font = `bold ${Math.max(10, 11 / tk)}px sans-serif`;
          ctx.fillStyle = "#ffffff";
          ctx.textAlign = "center";
          ctx.fillText(`[${pathIndex + 1}]`, n.x, n.y - n.radius - 7);
        }

        // Label
        const showLabel =
          isLocalGraph ||
          tk > 0.75 ||
          isSelected ||
          isHovered ||
          isInPath ||
          matchesSearch ||
          isHighlighted;

        if (showLabel) {
          ctx.font = `${Math.max(10, Math.min(12, 11 / tk))}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
          ctx.fillStyle = isSelected
            ? "#00893d"
            : isInPath
            ? "#d97706"
            : alpha < 0.5
            ? "rgba(100, 116, 139, 0.4)"
            : "#1e293b";
          ctx.textAlign = "center";
          ctx.fillText(
            n.name.replace(/^iRPS\s*/, ""),
            n.x,
            n.y + n.radius + (isLocalGraph ? 11 : 12)
          );
        }
      }

      ctx.restore();
      animFrameIdRef.current = requestAnimationFrame(render);
    };

    animFrameIdRef.current = requestAnimationFrame(render);

    return () => {
      isMounted = false;
      if (animFrameIdRef.current) cancelAnimationFrame(animFrameIdRef.current);
    };
  }, [links, selectedNodeId, filterCategory, searchKeyword, highlightedPath, isLocalGraph]);

  // Coordinate transforms
  const screenToWorld = useCallback((sx: number, sy: number) => {
    const { x: tx, y: ty, k: tk } = transformRef.current;
    return {
      x: (sx - tx) / tk,
      y: (sy - ty) / tk,
    };
  }, []);

  const findNodeAt = useCallback(
    (sx: number, sy: number): SimNode | null => {
      const { x: wx, y: wy } = screenToWorld(sx, sy);
      const simNodes = simNodesRef.current;
      for (let i = simNodes.length - 1; i >= 0; i--) {
        const n = simNodes[i];
        const dx = n.x - wx;
        const dy = n.y - wy;
        if (dx * dx + dy * dy <= (n.radius + 6) * (n.radius + 6)) {
          return n;
        }
      }
      return null;
    },
    [screenToWorld]
  );

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    const hit = findNodeAt(sx, sy);
    if (hit) {
      draggedNodeRef.current = hit;
    } else {
      isDraggingCanvasRef.current = true;
      dragStartRef.current = {
        x: e.clientX - transformRef.current.x,
        y: e.clientY - transformRef.current.y,
      };
    }
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    if (draggedNodeRef.current) {
      const { x: wx, y: wy } = screenToWorld(sx, sy);
      draggedNodeRef.current.x = wx;
      draggedNodeRef.current.y = wy;
      draggedNodeRef.current.vx = 0;
      draggedNodeRef.current.vy = 0;
      return;
    }

    if (isDraggingCanvasRef.current) {
      transformRef.current.x = e.clientX - dragStartRef.current.x;
      transformRef.current.y = e.clientY - dragStartRef.current.y;
      return;
    }

    const hit = findNodeAt(sx, sy);
    if (hit !== hoveredNodeRef.current) {
      hoveredNodeRef.current = hit;
      setHoveredNodeName(hit ? hit.name : null);
    }
  };

  const handleMouseUp = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (draggedNodeRef.current) {
      const rect = e.currentTarget.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      const hit = findNodeAt(sx, sy);
      if (hit && hit.id === draggedNodeRef.current.id) {
        onSelectNode(hit.id);
      }
    }
    draggedNodeRef.current = null;
    isDraggingCanvasRef.current = false;
  };

  const handleWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    const zoomFactor = e.deltaY < 0 ? 1.08 : 0.92;
    const rect = e.currentTarget.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;

    const { x: tx, y: ty, k: tk } = transformRef.current;
    const newK = Math.max(0.25, Math.min(3.0, tk * zoomFactor));

    transformRef.current = {
      k: newK,
      x: sx - (sx - tx) * (newK / tk),
      y: sy - (sy - ty) * (newK / tk),
    };
  };

  const handleResetView = () => {
    transformRef.current = { x: 0, y: 0, k: isLocalGraph ? 0.95 : 1 };
  };

  return (
    <div
      ref={containerRef}
      data-testid="force-graph-canvas"
      style={{
        width: "100%",
        height: "100%",
        position: "relative",
        overflow: "hidden",
        backgroundColor: isLocalGraph ? "var(--canvas, #f3f5f2)" : "#f8fafc",
        cursor: draggedNodeRef.current
          ? "grabbing"
          : isDraggingCanvasRef.current
          ? "move"
          : "grab",
      }}
    >
      <canvas
        ref={canvasRef}
        width={containerRef.current?.clientWidth || (isLocalGraph ? 320 : 900)}
        height={containerRef.current?.clientHeight || (isLocalGraph ? 210 : 650)}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onWheel={handleWheel}
        style={{ width: "100%", height: "100%", display: "block" }}
      />

      {/* Floating Hover Badge */}
      {hoveredNodeName && !isLocalGraph && (
        <div
          style={{
            position: "absolute",
            bottom: 16,
            left: 16,
            padding: "5px 10px",
            backgroundColor: "var(--paper, #ffffff)",
            border: "1px solid var(--line, #d9dfda)",
            borderRadius: "4px",
            fontSize: "12px",
            color: "var(--ink, #18211c)",
            pointerEvents: "none",
            boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
          }}
        >
          {hoveredNodeName}
        </div>
      )}

      {/* Reset View Button */}
      {!isLocalGraph && (
        <button
          type="button"
          onClick={handleResetView}
          style={{
            position: "absolute",
            top: 12,
            right: 12,
            padding: "4px 8px",
            backgroundColor: "var(--paper, #ffffff)",
            border: "1px solid var(--line, #d9dfda)",
            borderRadius: "4px",
            color: "var(--muted, #6f7a73)",
            fontSize: "11px",
            cursor: "pointer",
          }}
        >
          重置视角
        </button>
      )}
    </div>
  );
}
