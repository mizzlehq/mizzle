import { PlayIcon } from "lucide-react"
import { useState } from "react"

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
import { api, formatValue, type SqlResult } from "@/lib/api"

const READ_ONLY_SQL = /^\s*(select|with|explain|show|table|values)\b/i

export function SqlConsole({ readOnly }: { readOnly: boolean }) {
  const [text, setText] = useState("select now()")
  const [result, setResult] = useState<SqlResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [confirm, setConfirm] = useState(false)

  const execute = async () => {
    setConfirm(false)
    setRunning(true)
    try {
      setResult(await api.sql(text))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setResult(null)
    } finally {
      setRunning(false)
    }
  }

  // Statements that may change data ask first (the server blocks them in read-only mode).
  const run = () => {
    if (!text.trim() || running) return
    if (!readOnly && !READ_ONLY_SQL.test(text)) setConfirm(true)
    else void execute()
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
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
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Button onClick={run} disabled={running}>
            <PlayIcon />
            {running ? "Running…" : "Run"}
          </Button>
          <span>Ctrl/⌘ + Enter. One statement at a time.</span>
          {result && (
            <span className="ml-auto">
              {result.count} row{result.count === 1 ? "" : "s"}
              {result.truncated && " (showing first 1000)"}
            </span>
          )}
        </div>
      </div>
      {error && (
        <div role="alert" className="border-b bg-destructive/10 px-4 py-2 text-destructive text-sm">
          {error}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        {result && result.columns.length > 0 && (
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
        )}
        {result && result.columns.length === 0 && (
          <p className="p-4 text-muted-foreground text-sm">Done. The statement returned no rows.</p>
        )}
      </div>
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Run a statement that may change data?</AlertDialogTitle>
            <AlertDialogDescription>
              This statement is not a plain query. It can change or delete data, and it runs
              immediately.
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
