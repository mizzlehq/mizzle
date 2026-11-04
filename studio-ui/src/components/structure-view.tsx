import { KeyRoundIcon, LinkIcon, PencilIcon, PlusIcon, TrashIcon } from "lucide-react"
import { useState } from "react"

import { DdlDialog, type ColumnInfo } from "@/components/ddl-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { TableInfo } from "@/lib/api"

type DialogState =
  | { kind: "add_column" }
  | { kind: "alter_column"; column: ColumnInfo }
  | { kind: "drop_column"; column: ColumnInfo }
  | { kind: "drop_table" }
  | null

export function StructureView({
  table,
  readOnly,
  onChanged,
}: {
  table: TableInfo
  readOnly: boolean
  onChanged: () => void
}) {
  const [dialog, setDialog] = useState<DialogState>(null)
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
  const asInfo = (c: TableInfo["columns"][string]): ColumnInfo => ({
    name: c.name,
    type: c.type,
    not_null: c.not_null,
    default: c.default,
  })

  return (
    <div className="space-y-6 p-4">
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-medium text-sm">Columns</h2>
          {!readOnly && (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setDialog({ kind: "add_column" })}>
                <PlusIcon />
                Add column
              </Button>
              <Button variant="outline" size="sm" onClick={() => setDialog({ kind: "drop_table" })}>
                <TrashIcon />
                Drop table
              </Button>
            </div>
          )}
        </div>
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Null</TableHead>
                <TableHead>Default</TableHead>
                {!readOnly && <TableHead className="w-20" />}
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
                    {!readOnly && (
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            aria-label={`Edit column ${c.name}`}
                            onClick={() => setDialog({ kind: "alter_column", column: asInfo(c) })}
                          >
                            <PencilIcon />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            aria-label={`Drop column ${c.name}`}
                            onClick={() => setDialog({ kind: "drop_column", column: asInfo(c) })}
                          >
                            <TrashIcon />
                          </Button>
                        </div>
                      </TableCell>
                    )}
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

      {dialog && (
        <DdlDialog
          key={`${dialog.kind}:${"column" in dialog ? dialog.column.name : table.name}`}
          kind={dialog.kind}
          schema={table.schema}
          table={table.name}
          column={"column" in dialog ? dialog.column : undefined}
          open
          onOpenChange={(open) => {
            if (!open) setDialog(null)
          }}
          onApplied={onChanged}
        />
      )}
    </div>
  )
}
