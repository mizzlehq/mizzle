import { useEffect, useMemo, useState } from "react"

import { api, type DiagramEdge, type DiagramNode } from "@/lib/api"

const CARD_W = 240
const COL_GAP = 64
const ROW_GAP = 28
const HEADER_H = 40
const LINE_H = 22
const PAD = 10

function cardHeight(node: DiagramNode): number {
  return HEADER_H + node.columns.length * LINE_H + PAD
}

const nodeKey = (schema: string, name: string) => `${schema}.${name}`

/** A read-only map of the schema: one card per table, an arrow per foreign key. */
export function RelationshipDiagram() {
  const [nodes, setNodes] = useState<DiagramNode[]>([])
  const [edges, setEdges] = useState<DiagramEdge[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .relationships()
      .then((data) => {
        setNodes(data.tables)
        setEdges(data.relationships)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const layout = useMemo(() => {
    const cols = Math.max(1, Math.ceil(Math.sqrt(nodes.length)))
    const positions = new Map<string, { x: number; y: number; h: number }>()
    const colHeights = Array.from({ length: cols }, () => 0)
    nodes.forEach((node, i) => {
      const col = i % cols
      const h = cardHeight(node)
      const x = col * (CARD_W + COL_GAP)
      const y = colHeights[col]
      positions.set(nodeKey(node.schema, node.name), { x, y, h })
      colHeights[col] = y + h + ROW_GAP
    })
    return {
      positions,
      width: cols * CARD_W + (cols - 1) * COL_GAP,
      height: Math.max(0, ...colHeights),
    }
  }, [nodes])

  if (error) return <p className="p-4 text-destructive text-sm">{error}</p>
  if (nodes.length === 0) {
    return <p className="p-4 text-muted-foreground text-sm">No tables in this database yet.</p>
  }

  return (
    <div className="min-h-0 flex-1 overflow-auto p-6">
      <div className="relative" style={{ width: layout.width, height: layout.height }}>
        <svg
          className="pointer-events-none absolute inset-0 text-muted-foreground"
          width={layout.width}
          height={layout.height}
        >
          <title>Schema relationships</title>
          <defs>
            <marker
              id="fk-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
            </marker>
          </defs>
          {edges.map((edge, i) => {
            const fromKey = nodeKey(edge.from_schema, edge.from_table)
            const toKey = nodeKey(edge.to_schema, edge.to_table)
            const from = layout.positions.get(fromKey)
            const to = layout.positions.get(toKey)
            if (!from || !to) return null
            if (fromKey === toKey) {
              // Self-reference: loop out of the right edge.
              const x = from.x + CARD_W
              const y = from.y + from.h / 2
              return (
                <path
                  key={i}
                  d={`M ${x} ${y} c 60 0, 60 40, 0 40`}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  markerEnd="url(#fk-arrow)"
                />
              )
            }
            const x1 = from.x + CARD_W
            const y1 = from.y + from.h / 2
            const x2 = to.x
            const y2 = to.y + to.h / 2
            const mid = (x1 + x2) / 2
            return (
              <path
                key={i}
                d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                markerEnd="url(#fk-arrow)"
              />
            )
          })}
        </svg>
        {nodes.map((node) => {
          const pos = layout.positions.get(nodeKey(node.schema, node.name))
          if (!pos) return null
          return (
            <div
              key={nodeKey(node.schema, node.name)}
              className="absolute overflow-hidden rounded-lg border bg-card shadow-sm"
              style={{ left: pos.x, top: pos.y, width: CARD_W, height: pos.h }}
            >
              <div className="truncate border-b px-3 py-2 font-medium text-sm">
                <span className="text-muted-foreground">{node.schema}.</span>
                {node.name}
              </div>
              <ul className="px-3 py-1 font-mono text-xs">
                {node.columns.map((column) => (
                  <li key={column} className="truncate leading-6" title={column}>
                    {column}
                  </li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>
    </div>
  )
}
