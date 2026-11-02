import { useCallback, useEffect, useMemo, useState } from "react"
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  FilterIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react"
import { toast } from "sonner"
import { cn } from "cn"

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { RowDialog } from "@/components/row-dialog"
import {
  api,
  editText,
  formatValue,
  toDbText,
  type Filter,
  type FilterOp,
  type Row,
  type TableInfo,
} from "@/lib/api"

const PAGE_SIZE = 50

const OPS: { value: FilterOp; label: string; needsValue: boolean }[] = [
  { value: "eq", label: "=", needsValue: true },
  { value: "ne", label: "≠", needsValue: true },
  { value: "lt", label: "<", needsValue: true },
  { value: "lte", label: "≤", needsValue: true },
  { value: "gt", label: ">", needsValue: true },
  { value: "gte", label: "≥", needsValue: true },
  { value: "ilike", label: "contains", needsValue: true },
  { value: "is_null", label: "is null", needsValue: false },
  { value: "is_not_null", label: "is not null", needsValue: false },
]

type Editing = { row: number; column: string; text: string }

function keyOf(table: TableInfo, row: Row): Row {
  return Object.fromEntries((table.primary_key?.columns ?? []).map((c) => [c, row[c]]))
}

function FilterBar({
  table,
  filters,
  onChange,
}: {
  table: TableInfo
  filters: Filter[]
  onChange: (filters: Filter[]) => void
}) {
  const columns = Object.keys(table.columns)
  const [column, setColumn] = useState(columns[0] ?? "")
  const [op, setOp] = useState<FilterOp>("eq")
  const [value, setValue] = useState("")
  const needsValue = OPS.find((o) => o.value === op)?.needsValue ?? true
  const value_ = op === "ilike" ? `%${value}%` : value

  const add = () => {
    if (!column || (needsValue && value === "")) return
    onChange([...filters, needsValue ? { column, op, value: value_ } : { column, op }])
    setValue("")
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        value={column}
        onValueChange={(v) => setColumn(String(v))}
        items={columns.map((c) => ({ label: c, value: c }))}
      >
        <SelectTrigger size="sm" aria-label="Filter column">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {columns.map((c) => (
            <SelectItem key={c} value={c}>
              {c}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={op}
        onValueChange={(v) => setOp(v as FilterOp)}
        items={OPS.map((o) => ({ label: o.label, value: o.value }))}
      >
        <SelectTrigger size="sm" aria-label="Filter operator">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {OPS.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {needsValue && (
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="value"
          aria-label="Filter value"
          className="h-7 w-40"
        />
      )}
      <Button size="sm" variant="outline" onClick={add}>
        <FilterIcon />
        Add filter
      </Button>
      {filters.map((f, i) => (
        <Badge key={i} variant="secondary" className="gap-1">
          {f.column} {OPS.find((o) => o.value === f.op)?.label} {f.value ?? ""}
          <button
            type="button"
            aria-label="Remove filter"
            onClick={() => onChange(filters.filter((_, j) => j !== i))}
          >
            <XIcon className="size-3" />
          </button>
        </Badge>
      ))}
    </div>
  )
}

export function DataGrid({ table, readOnly }: { table: TableInfo; readOnly: boolean }) {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [order, setOrder] = useState<{ column: string; dir: "asc" | "desc" } | null>(null)
  const [filters, setFilters] = useState<Filter[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [editing, setEditing] = useState<Editing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const columns = useMemo(() => Object.values(table.columns), [table])
  const editable = !readOnly && table.primary_key !== null

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.rows(table, {
        limit: PAGE_SIZE,
        offset,
        order: order?.column,
        dir: order?.dir,
        filters,
      })
      setRows(data.rows)
      setTotal(data.total)
      setSelected(new Set())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [table, offset, order, filters])

  useEffect(() => {
    void load()
  }, [load])

  const sortBy = (column: string) => {
    setOffset(0)
    setOrder((current) =>
      current?.column !== column
        ? { column, dir: "asc" }
        : current.dir === "asc"
          ? { column, dir: "desc" }
          : null
    )
  }

  const saveEdit = async (value: string | null) => {
    if (!editing) return
    const row = rows[editing.row]
    const column = table.columns[editing.column]
    try {
      const text = value === null ? null : toDbText(column, value)
      const { row: updated } = await api.update(table, keyOf(table, row), {
        [editing.column]: text,
      })
      setRows((current) => current.map((r, i) => (i === editing.row ? updated : r)))
      setEditing(null)
      toast.success("Row updated")
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  const deleteSelected = async () => {
    try {
      const keys = [...selected].map((i) => keyOf(table, rows[i]))
      const { deleted } = await api.remove(table, keys)
      toast.success(`Deleted ${deleted} row${deleted === 1 ? "" : "s"}`)
      setConfirmDelete(false)
      await load()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  const allSelected = rows.length > 0 && selected.size === rows.length

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <FilterBar
          table={table}
          filters={filters}
          onChange={(f) => {
            setOffset(0)
            setFilters(f)
          }}
        />
        <div className="ml-auto flex items-center gap-2">
          {editable && selected.size > 0 && (
            <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)}>
              <Trash2Icon />
              Delete {selected.size}
            </Button>
          )}
          {!readOnly && (
            <Button size="sm" onClick={() => setAdding(true)}>
              <PlusIcon />
              Add row
            </Button>
          )}
          <Button size="icon-sm" variant="ghost" onClick={() => void load()} aria-label="Reload rows">
            <RefreshCwIcon className={cn(loading && "animate-spin")} />
          </Button>
        </div>
      </div>

      {error && (
        <div role="alert" className="border-b bg-destructive/10 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {!readOnly && table.primary_key === null && (
        <div className="border-b bg-muted px-4 py-2 text-xs text-muted-foreground">
          This table has no primary key, so rows cannot be edited or deleted here. Use the SQL
          console.
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <Table className="font-mono text-xs">
          <TableHeader className="sticky top-0 z-10 bg-background">
            <TableRow>
              {editable && (
                <TableHead className="w-8">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={(checked) =>
                      setSelected(checked ? new Set(rows.map((_, i) => i)) : new Set())
                    }
                    aria-label="Select all rows"
                  />
                </TableHead>
              )}
              {columns.map((c) => (
                <TableHead key={c.name}>
                  <button
                    type="button"
                    onClick={() => sortBy(c.name)}
                    className="inline-flex items-center gap-1 font-sans font-medium"
                    title={c.type}
                  >
                    {c.name}
                    <span className="font-normal text-muted-foreground">{c.type}</span>
                    {order?.column === c.name &&
                      (order.dir === "asc" ? (
                        <ArrowUpIcon className="size-3" />
                      ) : (
                        <ArrowDownIcon className="size-3" />
                      ))}
                  </button>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, i) => (
              <TableRow key={i} data-state={selected.has(i) ? "selected" : undefined}>
                {editable && (
                  <TableCell>
                    <Checkbox
                      checked={selected.has(i)}
                      onCheckedChange={(checked) =>
                        setSelected((current) => {
                          const next = new Set(current)
                          if (checked) next.add(i)
                          else next.delete(i)
                          return next
                        })
                      }
                      aria-label={`Select row ${i + 1}`}
                    />
                  </TableCell>
                )}
                {columns.map((c) => {
                  const value = row[c.name]
                  const isEditing = editing?.row === i && editing.column === c.name
                  return (
                    <TableCell
                      key={c.name}
                      className="max-w-80 truncate"
                      title={formatValue(value)}
                      onDoubleClick={() =>
                        editable && setEditing({ row: i, column: c.name, text: editText(value) })
                      }
                    >
                      {isEditing ? (
                        <div className="flex items-center gap-1">
                          <Input
                            autoFocus
                            value={editing.text}
                            onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void saveEdit(editing.text)
                              if (e.key === "Escape") setEditing(null)
                            }}
                            aria-label={`Edit ${c.name}`}
                            className="h-7 min-w-40 font-mono text-xs"
                          />
                          {!c.not_null && (
                            <Button size="xs" variant="outline" onClick={() => void saveEdit(null)}>
                              NULL
                            </Button>
                          )}
                        </div>
                      ) : value === null || value === undefined ? (
                        <span className="text-muted-foreground italic">NULL</span>
                      ) : (
                        formatValue(value)
                      )}
                    </TableCell>
                  )
                })}
              </TableRow>
            ))}
            {rows.length === 0 && !loading && (
              <TableRow>
                <TableCell
                  colSpan={columns.length + (editable ? 1 : 0)}
                  className="py-10 text-center font-sans text-muted-foreground"
                >
                  No rows.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex items-center gap-2 border-t px-4 py-2 text-sm text-muted-foreground">
        {editable && <span>Double-click a cell to edit it.</span>}
        <span className="ml-auto">
          {total === 0 ? "0 rows" : `${offset + 1}–${offset + rows.length} of ${total}`}
        </span>
        <Button
          size="icon-sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          aria-label="Previous page"
        >
          <ChevronLeftIcon />
        </Button>
        <Button
          size="icon-sm"
          variant="outline"
          disabled={offset + PAGE_SIZE >= total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
          aria-label="Next page"
        >
          <ChevronRightIcon />
        </Button>
      </div>

      <RowDialog
        table={table}
        open={adding}
        onOpenChange={setAdding}
        onCreated={() => {
          setAdding(false)
          void load()
        }}
      />
      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete {selected.size} row{selected.size === 1 ? "" : "s"}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes the rows from {table.name}. It cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button variant="destructive" onClick={() => void deleteSelected()}>
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
