import { PlusIcon, TrashIcon } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { api, type DdlColumnInput, type DdlOperation, type DdlStatement } from "@/lib/api"

export type DdlKind = "create_table" | "add_column" | "alter_column" | "drop_table" | "drop_column"

type ColumnDraft = {
  name: string
  type: string
  not_null: boolean
  primary_key: boolean
  default: string
}

export type ColumnInfo = { name: string; type: string; not_null: boolean; default: string | null }

type Props = {
  kind: DdlKind
  schema: string
  table: string
  column?: ColumnInfo
  open: boolean
  onOpenChange: (open: boolean) => void
  onApplied: () => void
}

const emptyColumn = (): ColumnDraft => ({
  name: "",
  type: "text",
  not_null: false,
  primary_key: false,
  default: "",
})

function toInput(draft: ColumnDraft): DdlColumnInput {
  const input: DdlColumnInput = {
    name: draft.name.trim(),
    type: draft.type.trim(),
    not_null: draft.not_null,
  }
  if (draft.primary_key) input.primary_key = true
  if (draft.default.trim()) input.default = draft.default.trim()
  return input
}

const TITLES: Record<DdlKind, string> = {
  create_table: "New table",
  add_column: "Add column",
  alter_column: "Edit column",
  drop_table: "Drop table",
  drop_column: "Drop column",
}

export function DdlDialog({ kind, schema, table, column, open, onOpenChange, onApplied }: Props) {
  const [tableName, setTableName] = useState(table)
  const [columns, setColumns] = useState<ColumnDraft[]>([emptyColumn()])
  const [draft, setDraft] = useState<ColumnDraft>(
    column ? { ...column, primary_key: false, default: column.default ?? "" } : emptyColumn()
  )
  const [statements, setStatements] = useState<DdlStatement[] | null>(null)
  const [busy, setBusy] = useState(false)

  const build = (): DdlOperation | string => {
    if (kind === "create_table") {
      if (!tableName.trim()) return "Table name is required"
      const cols = columns.filter((c) => c.name.trim() && c.type.trim())
      if (cols.length === 0) return "Add at least one column"
      return {
        operation: "create_table",
        schema,
        name: tableName.trim(),
        columns: cols.map(toInput),
      }
    }
    if (kind === "add_column") {
      if (!draft.name.trim() || !draft.type.trim()) return "Column name and type are required"
      return {
        operation: "add_column",
        schema,
        table,
        name: draft.name.trim(),
        column: toInput(draft),
      }
    }
    if (kind === "alter_column") {
      const changes: { type?: string; not_null?: boolean; default?: string | null } = {}
      const wanted = draft.default.trim() === "" ? null : draft.default.trim()
      if (column && draft.type.trim() && draft.type !== column.type)
        changes.type = draft.type.trim()
      if (column && draft.not_null !== column.not_null) changes.not_null = draft.not_null
      if (column && wanted !== column.default) changes.default = wanted
      if (Object.keys(changes).length === 0) return "Change a field first"
      return { operation: "alter_column", schema, table, name: draft.name, ...changes }
    }
    if (kind === "drop_table") return { operation: "drop_table", schema, name: table }
    return { operation: "drop_column", schema, table, name: draft.name }
  }

  const preview = async () => {
    const request = build()
    if (typeof request === "string") {
      toast.error(request)
      return
    }
    setBusy(true)
    try {
      setStatements((await api.ddlPreview(request)).statements)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const apply = async () => {
    const request = build()
    if (typeof request === "string") return
    setBusy(true)
    try {
      const result = await api.ddlApply(request)
      toast.success(`Applied ${result.applied} statement${result.applied === 1 ? "" : "s"}`)
      onApplied()
      onOpenChange(false)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const edit = (index: number, patch: Partial<ColumnDraft>) =>
    setColumns(columns.map((c, i) => (i === index ? { ...c, ...patch } : c)))

  const columnFields = (value: ColumnDraft, onChange: (patch: Partial<ColumnDraft>) => void) => (
    <div className="grid gap-3">
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Name</span>
        <Input
          value={value.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="column_name"
          className="font-mono text-xs"
        />
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Type</span>
        <Input
          value={value.type}
          onChange={(e) => onChange({ type: e.target.value })}
          placeholder="text, numeric(10,2), timestamp with time zone…"
          className="font-mono text-xs"
        />
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Default</span>
        <Input
          value={value.default}
          onChange={(e) => onChange({ default: e.target.value })}
          placeholder="now(), 0, 'x'… (optional)"
          className="font-mono text-xs"
        />
      </label>
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={value.not_null}
            onCheckedChange={(checked) => onChange({ not_null: checked === true })}
            aria-label="Not null"
          />
          Not null
        </div>
        {(kind === "create_table" || kind === "add_column") && (
          <div className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={value.primary_key}
              onCheckedChange={(checked) => onChange({ primary_key: checked === true })}
              aria-label="Primary key"
            />
            Primary key
          </div>
        )}
      </div>
    </div>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{TITLES[kind]}</DialogTitle>
          <DialogDescription>
            {statements
              ? "Review the SQL Studio generated before applying it."
              : kind === "drop_table"
                ? `This drops ${schema}.${table} and all of its rows.`
                : kind === "drop_column"
                  ? `This drops ${table}.${draft.name} and its data.`
                  : "Studio generates the DDL from your input; it never runs free-form SQL here."}
          </DialogDescription>
        </DialogHeader>

        <div className="my-2 max-h-[60vh] space-y-3 overflow-y-auto pr-1">
          {statements ? (
            statements.map((statement) => (
              <div key={statement.sql} className="rounded-md border p-2">
                <div className="mb-1 flex items-center gap-2 text-muted-foreground text-xs">
                  <span>{statement.description}</span>
                  {statement.data_loss && <Badge variant="destructive">loses data</Badge>}
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs">
                  {statement.sql}
                </pre>
              </div>
            ))
          ) : kind === "create_table" ? (
            <>
              <label className="grid gap-1 text-sm">
                <span className="font-medium">Table name</span>
                <Input
                  value={tableName}
                  onChange={(e) => setTableName(e.target.value)}
                  placeholder="table_name"
                  className="font-mono text-xs"
                />
              </label>
              {columns.map((c, i) => (
                <div key={i} className="rounded-md border p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-medium text-sm">Column {i + 1}</span>
                    {columns.length > 1 && (
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Remove column"
                        onClick={() => setColumns(columns.filter((_, j) => j !== i))}
                      >
                        <TrashIcon />
                      </Button>
                    )}
                  </div>
                  {columnFields(c, (patch) => edit(i, patch))}
                </div>
              ))}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setColumns([...columns, emptyColumn()])}
              >
                <PlusIcon />
                Add column
              </Button>
            </>
          ) : kind === "drop_table" || kind === "drop_column" ? (
            <p className="text-sm">
              Press <span className="font-medium">Review</span> to see the exact SQL, then{" "}
              <span className="font-medium">Apply</span> to run it.
            </p>
          ) : (
            columnFields(draft, (patch) => setDraft({ ...draft, ...patch }))
          )}
        </div>

        <DialogFooter>
          {statements ? (
            <>
              <Button variant="outline" onClick={() => setStatements(null)} disabled={busy}>
                Back
              </Button>
              <Button variant="destructive" onClick={() => void apply()} disabled={busy}>
                {busy ? "Applying…" : "Apply"}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
                Cancel
              </Button>
              <Button onClick={() => void preview()} disabled={busy}>
                {busy ? "Generating…" : "Review"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
