import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { api, toDbText, type Row, type TableInfo } from "@/lib/api"

type Props = {
  table: TableInfo
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: () => void
}

export function RowDialog({ table, open, onOpenChange, onCreated }: Props) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const columns = Object.values(table.columns)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSaving(true)
    try {
      // Empty fields are left out, so the database default (or NULL) applies.
      const row: Row = {}
      for (const column of columns) {
        const text = values[column.name]
        if (text !== undefined && text !== "") row[column.name] = toDbText(column, text)
      }
      await api.insert(table, row)
      toast.success("Row added")
      setValues({})
      onCreated()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={(e) => void submit(e)}>
          <DialogHeader>
            <DialogTitle>Add row to {table.name}</DialogTitle>
            <DialogDescription>
              Leave a field empty to use its default. Arrays and JSON take JSON text.
            </DialogDescription>
          </DialogHeader>
          <div className="my-4 grid max-h-[60vh] gap-3 overflow-y-auto pr-1">
            {columns.map((column) => (
              <label key={column.name} className="grid gap-1 text-sm">
                <span className="flex items-baseline gap-2">
                  <span className="font-medium">{column.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">{column.type}</span>
                  {column.not_null && column.default === null && !column.type.endsWith("serial") && (
                    <span className="text-xs text-destructive">required</span>
                  )}
                </span>
                <Input
                  value={values[column.name] ?? ""}
                  onChange={(e) => setValues({ ...values, [column.name]: e.target.value })}
                  placeholder={column.default ? `default: ${column.default}` : ""}
                  className="font-mono text-xs"
                />
              </label>
            ))}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Add row"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
