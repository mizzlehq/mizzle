import { useMemo, useState } from "react"
import { DatabaseIcon, RefreshCwIcon, SearchIcon, TableIcon, TerminalIcon } from "lucide-react"
import { cn } from "cn"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { tableKey, type TableInfo } from "@/lib/api"

type Props = {
  tables: TableInfo[]
  selected: string | null
  sqlOpen: boolean
  readOnly: boolean
  onSelect: (key: string) => void
  onOpenSql: () => void
  onRefresh: () => void
}

export function TableSidebar({
  tables,
  selected,
  sqlOpen,
  readOnly,
  onSelect,
  onOpenSql,
  onRefresh,
}: Props) {
  const [search, setSearch] = useState("")
  const groups = useMemo(() => {
    const needle = search.trim().toLowerCase()
    const bySchema = new Map<string, TableInfo[]>()
    for (const table of tables) {
      if (needle && !table.name.toLowerCase().includes(needle)) continue
      bySchema.set(table.schema, [...(bySchema.get(table.schema) ?? []), table])
    }
    return [...bySchema.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [tables, search])

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-2 px-4 py-3">
        <DatabaseIcon className="size-4 text-muted-foreground" />
        <span className="font-heading font-semibold">mizzle studio</span>
        {readOnly && (
          <Badge variant="secondary" className="ml-auto">
            read-only
          </Badge>
        )}
      </div>
      <div className="flex gap-1 px-3 pb-2">
        <div className="relative flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tables"
            aria-label="Search tables"
            className="pl-7"
          />
        </div>
        <Button variant="ghost" size="icon" onClick={onRefresh} aria-label="Reload tables">
          <RefreshCwIcon />
        </Button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <nav className="px-2 pb-2">
          {groups.map(([schema, items]) => (
            <div key={schema} className="mb-2">
              <div className="px-2 py-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                {schema}
              </div>
              {items.map((table) => {
                const key = tableKey(table)
                const active = !sqlOpen && key === selected
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => onSelect(key)}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-sidebar-accent",
                      active && "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                    )}
                  >
                    <TableIcon className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="truncate">{table.name}</span>
                  </button>
                )
              })}
            </div>
          ))}
          {groups.length === 0 && (
            <p className="px-2 py-4 text-sm text-muted-foreground">No tables found.</p>
          )}
        </nav>
      </ScrollArea>
      <div className="border-t p-2">
        <Button
          variant={sqlOpen ? "secondary" : "ghost"}
          className="w-full justify-start"
          onClick={onOpenSql}
        >
          <TerminalIcon />
          SQL console
        </Button>
      </div>
    </aside>
  )
}
