import { HistoryIcon, PlayIcon, SaveIcon, Trash2Icon } from "lucide-react"
import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import {
  api,
  formatValue,
  type HistoryEntry,
  type SavedQuery,
  type SqlStatementResult,
} from "@/lib/api"

const READ_ONLY_SQL = /^\s*(select|with|explain|show|table|values)\b/i
const PREVIEW = 60

type Panel = "history" | "saved" | null

function summarize(sql: string): string {
  const oneLine = sql.replace(/\s+/g, " ").trim()
  return oneLine.length > PREVIEW ? `${oneLine.slice(0, PREVIEW)}…` : oneLine
}

function ResultTable({ result }: { result: SqlStatementResult }) {
  if (result.columns.length === 0) {
    return (
      <p className="px-4 py-2 text-muted-foreground text-sm">
        Done. The statement returned no rows.
      </p>
    )
  }
  return (
    <Table className="font-mono text-xs">
      <TableHeader className="sticky top-0 bg-background">
        <TableRow>
          {result.columns.map((c) => (
            <TableHead key={c} className="font-sans">
              {c}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {result.rows.map((row, i) => (
          <TableRow key={i}>
            {row.map((value, j) => (
              <TableCell key={j} className="max-w-80 truncate" title={formatValue(value)}>
                {value === null ? (
                  <span className="text-muted-foreground italic">NULL</span>
                ) : (
                  formatValue(value)
                )}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function SqlConsole({ readOnly }: { readOnly: boolean }) {
  const [text, setText] = useState("select now()")
  const [results, setResults] = useState<SqlStatementResult[]>([])
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [panel, setPanel] = useState<Panel>(null)
  const [history, setHistory] = useState<HistoryEntry[]>([])
  const [saved, setSaved] = useState<SavedQuery[]>([])

  const refresh = useCallback(async () => {
    try {
      const [h, s] = await Promise.all([api.history(), api.saved()])
      setHistory(h.history)
      setSaved(s.saved)
    } catch {
      // History and saved queries are a convenience; never block the console on them.
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const execute = async () => {
    setConfirm(false)
    setRunning(true)
    try {
      const batch = await api.sql(text)
      setResults(batch.results)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setResults([])
    } finally {
      setRunning(false)
      void refresh()
    }
  }

  // Statements that may change data ask first (the server blocks them in read-only mode).
  const statements = text
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean)
  const onlyReads = statements.length > 0 && statements.every((s) => READ_ONLY_SQL.test(s))
  const run = () => {
    if (!text.trim() || running) return
    if (!readOnly && !onlyReads) setConfirm(true)
    else void execute()
  }

  const save = async () => {
    const name = window.prompt("Name this query")
    if (!name?.trim()) return
    try {
      await api.saveQuery(name.trim(), text)
      toast.success(`Saved "${name.trim()}"`)
      await refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  const remove = async (name: string) => {
    try {
      await api.deleteQuery(name)
      await refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e))
    }
  }

  const totalRows = results.reduce((sum, r) => sum + r.count, 0)
  const names = results.length === 1 ? "" : `${results.length} statements, `

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="space-y-2 border-b p-4">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                run()
              }
            }}
            spellCheck={false}
            aria-label="SQL"
            className="min-h-32 font-mono text-sm"
          />
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground text-sm">
            <Button onClick={run} disabled={running}>
              <PlayIcon />
              {running ? "Running…" : "Run"}
            </Button>
            <Button variant="outline" onClick={() => void save()}>
              <SaveIcon />
              Save
            </Button>
            <Button
              variant="outline"
              aria-pressed={panel === "history"}
              onClick={() => setPanel(panel === "history" ? null : "history")}
            >
              <HistoryIcon />
              History
            </Button>
            <Button
              variant="outline"
              aria-pressed={panel === "saved"}
              onClick={() => setPanel(panel === "saved" ? null : "saved")}
            >
              Saved
            </Button>
            <span>Ctrl/⌘ + Enter. Separate statements with semicolons.</span>
            {results.length > 0 && (
              <span className="ml-auto">
                {names}
                {totalRows}
                {results.some((r) => r.truncated) ? "+" : ""} row{totalRows === 1 ? "" : "s"}
                {results.some((r) => r.truncated) && " (showing first 1000 per statement)"}
              </span>
            )}
          </div>
        </div>
        {error && (
          <div
            role="alert"
            className="border-b bg-destructive/10 px-4 py-2 text-destructive text-sm"
          >
            {error}
          </div>
        )}
        <div className="min-h-0 flex-1 divide-y overflow-auto">
          {results.map((result, i) => (
            <section key={i}>
              <div className="sticky top-0 z-10 border-b bg-muted px-4 py-1.5 font-mono text-muted-foreground text-xs">
                {results.length > 1 ? `${i + 1}. ` : ""}
                {summarize(result.statement)}
              </div>
              <ResultTable result={result} />
            </section>
          ))}
          {results.length === 0 && !error && (
            <p className="p-4 text-muted-foreground text-sm">
              Run a query to see results. Several statements run in order.
            </p>
          )}
        </div>
      </div>

      {panel && (
        <aside className="flex w-72 min-w-0 shrink-0 flex-col border-l">
          <header className="flex items-center justify-between border-b px-3 py-2">
            <h2 className="font-medium text-sm">{panel === "history" ? "History" : "Saved"}</h2>
            {panel === "history" && history.length > 0 && (
              <Button
                size="xs"
                variant="ghost"
                onClick={async () => {
                  await api.clearHistory()
                  await refresh()
                }}
              >
                Clear
              </Button>
            )}
          </header>
          <div className="min-h-0 flex-1 overflow-auto p-2">
            {panel === "history" &&
              history
                .slice()
                .reverse()
                .map((entry, i) => (
                  <button
                    type="button"
                    key={i}
                    onClick={() => setText(entry.sql)}
                    className="block w-full truncate rounded px-2 py-1.5 text-left font-mono text-xs hover:bg-muted"
                    title={entry.sql}
                  >
                    {!entry.ok && <span className="text-destructive">● </span>}
                    {summarize(entry.sql)}
                  </button>
                ))}
            {panel === "history" && history.length === 0 && (
              <p className="p-2 text-muted-foreground text-xs">No queries run yet.</p>
            )}
            {panel === "saved" &&
              saved.map((query) => (
                <div key={query.name} className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => setText(query.sql)}
                    className="min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left text-xs hover:bg-muted"
                    title={query.sql}
                  >
                    {query.name}
                  </button>
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    aria-label={`Delete ${query.name}`}
                    onClick={() => void remove(query.name)}
                  >
                    <Trash2Icon />
                  </Button>
                </div>
              ))}
            {panel === "saved" && saved.length === 0 && (
              <p className="p-2 text-muted-foreground text-xs">
                No saved queries. Name one with Save.
              </p>
            )}
          </div>
        </aside>
      )}

      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Run a statement that may change data?</AlertDialogTitle>
            <AlertDialogDescription>
              One or more statements are not plain queries. They can change or delete data, and they
              run immediately.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button variant="destructive" onClick={() => void execute()}>
              Run it
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
