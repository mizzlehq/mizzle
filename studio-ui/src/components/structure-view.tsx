import { KeyRoundIcon, LinkIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { TableInfo } from "@/lib/api"

export function StructureView({ table }: { table: TableInfo }) {
  const pk = new Set(table.primary_key?.columns ?? [])
  const unique = new Set(
    Object.values(table.uniques)
      .filter((u) => u.columns.length === 1)
      .map((u) => u.columns[0])
  )
  const fkByColumn = new Map(
    Object.values(table.foreign_keys)
      .filter((fk) => fk.columns.length === 1)
      .map((fk) => [fk.columns[0], fk])
  )
  const constraints = [
    ...Object.values(table.foreign_keys).map((fk) => ({
      name: fk.name,
      kind: "foreign key",
      detail: `(${fk.columns.join(", ")}) → ${fk.ref_table} (${fk.ref_columns.join(", ")})${
        fk.on_delete ? ` on delete ${fk.on_delete}` : ""
      }`,
    })),
    ...Object.values(table.uniques).map((u) => ({
      name: u.name,
      kind: "unique",
      detail: `(${u.columns.join(", ")})`,
    })),
    ...Object.values(table.checks).map((c) => ({
      name: c.name,
      kind: "check",
      detail: c.expression,
    })),
    ...Object.values(table.indexes).map((i) => ({
      name: i.name,
      kind: i.unique ? "unique index" : "index",
      detail: `(${i.columns.join(", ")})${i.where ? ` where ${i.where}` : ""}`,
    })),
  ]

  return (
    <div className="space-y-6 p-4">
      <section>
        <h2 className="mb-2 font-medium text-sm">Columns</h2>
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Null</TableHead>
                <TableHead>Default</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Object.values(table.columns).map((c) => {
                const fk = fkByColumn.get(c.name)
                return (
                  <TableRow key={c.name}>
                    <TableCell className="font-medium">
                      <span className="inline-flex items-center gap-1.5">
                        {pk.has(c.name) && (
                          <KeyRoundIcon
                            className="size-3.5 text-amber-500"
                            aria-label="primary key"
                          />
                        )}
                        {c.name}
                        {unique.has(c.name) && <Badge variant="outline">unique</Badge>}
                        {fk && (
                          <Badge variant="outline" className="gap-1">
                            <LinkIcon className="size-3" />
                            {fk.ref_table}.{fk.ref_columns[0]}
                          </Badge>
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{c.type}</TableCell>
                    <TableCell>{c.not_null ? "no" : "yes"}</TableCell>
                    <TableCell className="font-mono text-muted-foreground text-xs">
                      {c.default ?? "—"}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      </section>
      <section>
        <h2 className="mb-2 font-medium text-sm">Constraints and indexes</h2>
        {constraints.length === 0 ? (
          <p className="text-muted-foreground text-sm">None.</p>
        ) : (
          <div className="rounded-lg border">
            <Table>
              <TableBody>
                {constraints.map((c) => (
                  <TableRow key={`${c.kind}:${c.name}`}>
                    <TableCell className="font-medium">{c.name}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{c.kind}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{c.detail}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>
    </div>
  )
}
