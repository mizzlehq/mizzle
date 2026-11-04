import { DatabaseIcon } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { DataGrid } from "@/components/data-grid"
import { RelationshipDiagram } from "@/components/relationship-diagram"
import { SqlConsole } from "@/components/sql-console"
import { StructureView } from "@/components/structure-view"
import { TableSidebar, type View } from "@/components/table-sidebar"
import { Toaster } from "@/components/ui/sonner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ApiError, api, type Meta, type TableInfo, tableKey } from "@/lib/api"

export function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [tables, setTables] = useState<TableInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [view, setView] = useState<View>("table")
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [m, t] = await Promise.all([api.meta(), api.tables()])
      setMeta(m)
      setTables(t.tables)
      setSelected((current) => current ?? (t.tables[0] ? tableKey(t.tables[0]) : null))
      setError(null)
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? "Not authorized. Open the link that `mizzle studio` printed in your terminal."
          : e instanceof Error
            ? e.message
            : String(e)
      )
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const table = tables.find((t) => tableKey(t) === selected) ?? null
  const readOnly = meta?.read_only ?? true

  if (error && !meta) {
    return (
      <div className="flex h-svh items-center justify-center bg-background p-6 text-foreground">
        <div className="max-w-md space-y-2 text-center">
          <DatabaseIcon className="mx-auto size-8 text-muted-foreground" />
          <h1 className="font-heading font-semibold text-lg">mizzle studio</h1>
          <p className="text-muted-foreground text-sm">{error}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-svh bg-background text-foreground">
      <TableSidebar
        tables={tables}
        selected={selected}
        view={view}
        readOnly={readOnly}
        onSelect={(key) => {
          setSelected(key)
          setView("table")
        }}
        onOpenSql={() => setView("sql")}
        onOpenDiagram={() => setView("diagram")}
        onRefresh={() => void load()}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        {view === "sql" ? (
          <>
            <header className="border-b px-4 py-3">
              <h1 className="font-heading font-semibold">SQL console</h1>
            </header>
            <SqlConsole readOnly={readOnly} />
          </>
        ) : view === "diagram" ? (
          <>
            <header className="border-b px-4 py-3">
              <h1 className="font-heading font-semibold">Schema diagram</h1>
            </header>
            <RelationshipDiagram />
          </>
        ) : table ? (
          <Tabs
            key={tableKey(table)}
            defaultValue="data"
            className="flex min-h-0 flex-1 flex-col gap-0"
          >
            <header className="flex items-center gap-4 border-b px-4 py-2">
              <h1 className="font-heading font-semibold">
                <span className="text-muted-foreground">{table.schema}.</span>
                {table.name}
              </h1>
              <TabsList>
                <TabsTrigger value="data">Data</TabsTrigger>
                <TabsTrigger value="structure">Structure</TabsTrigger>
              </TabsList>
            </header>
            <TabsContent value="data" className="flex min-h-0 flex-1 flex-col">
              <DataGrid table={table} readOnly={readOnly} />
            </TabsContent>
            <TabsContent value="structure" className="min-h-0 flex-1 overflow-auto">
              <StructureView table={table} />
            </TabsContent>
          </Tabs>
        ) : (
          <div className="flex flex-1 items-center justify-center text-muted-foreground text-sm">
            No tables in this database yet. Run `mizzle push` or `mizzle migrate`.
          </div>
        )}
      </main>
      <Toaster position="bottom-right" />
    </div>
  )
}

export default App
