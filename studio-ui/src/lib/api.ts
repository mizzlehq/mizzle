export type Column = {
  name: string
  type: string
  not_null: boolean
  default: string | null
}

export type ForeignKey = {
  name: string
  columns: string[]
  ref_schema: string
  ref_table: string
  ref_columns: string[]
  on_delete: string | null
  on_update: string | null
}

export type TableInfo = {
  schema: string
  name: string
  columns: Record<string, Column>
  primary_key: { name: string; columns: string[] } | null
  uniques: Record<string, { name: string; columns: string[] }>
  checks: Record<string, { name: string; expression: string }>
  foreign_keys: Record<string, ForeignKey>
  indexes: Record<
    string,
    { name: string; columns: string[]; unique: boolean; where: string | null }
  >
  /** S4-9: columns whose type has no ordering. */
  unsortable?: string[]
  /** S5-2: composite columns, shown but not edited in the grid. */
  read_only?: string[]
}

export type Meta = { read_only: boolean; dialect: string }
export type Row = Record<string, unknown>
export type FilterOp =
  | "eq"
  | "ne"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "like"
  | "ilike"
  | "is_null"
  | "is_not_null"
export type Filter = { column: string; op: FilterOp; value?: string }
export type SqlResult = {
  columns: string[]
  rows: unknown[][]
  truncated: boolean
  count: number
}

/** One statement's result inside a multi-statement run. */
export type SqlStatementResult = SqlResult & { statement: string }
export type SqlBatch = { results: SqlStatementResult[] }
export type HistoryEntry = { sql: string; at: string; ok: boolean }
export type SavedQuery = { name: string; sql: string }
export type DiagramNode = { schema: string; name: string; columns: string[] }
export type DiagramEdge = {
  name: string
  from_schema: string
  from_table: string
  from_columns: string[]
  to_schema: string
  to_table: string
  to_columns: string[]
}

export type DdlColumnInput = {
  name: string
  type: string
  not_null?: boolean
  primary_key?: boolean
  default?: string | null
}

export type DdlOperation =
  | { operation: "create_table"; schema: string; name: string; columns: DdlColumnInput[] }
  | { operation: "drop_table"; schema: string; name: string }
  | { operation: "add_column"; schema: string; table: string; name: string; column: DdlColumnInput }
  | {
      operation: "alter_column"
      schema: string
      table: string
      name: string
      type?: string
      not_null?: boolean
      default?: string | null
    }
  | { operation: "drop_column"; schema: string; table: string; name: string }

export type DdlStatement = { sql: string; description: string; data_loss: boolean }
export type DdlPreview = { statements: DdlStatement[] }
export type DdlResult = { applied: number; statements: DdlStatement[] }

const TOKEN_KEY = "mizzle-studio-token"

// The token arrives in the URL fragment (never sent to servers or logged). Keep it for
// this tab only and remove it from the address bar so it is not shared by accident.
function readToken(): string {
  const fromHash = new URLSearchParams(window.location.hash.slice(1)).get("token")
  try {
    if (fromHash) {
      sessionStorage.setItem(TOKEN_KEY, fromHash)
      window.history.replaceState(null, "", window.location.pathname)
    }
    return sessionStorage.getItem(TOKEN_KEY) ?? fromHash ?? ""
  } catch {
    return fromHash ?? ""
  }
}

const token = readToken()

// With no token (a remote Studio behind --auth), send no Authorization header: setting
// one stops the browser from answering the Basic auth challenge itself.
const auth: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: {
      ...auth,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data: unknown = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as { error: unknown }).error)
        : response.statusText
    throw new ApiError(message, response.status)
  }
  return data as T
}

async function requestBlob(path: string): Promise<Blob> {
  const response = await fetch(path, { headers: auth })
  if (!response.ok) {
    const data: unknown = await response.json().catch(() => ({}))
    const message =
      typeof data === "object" && data !== null && "error" in data
        ? String((data as { error: unknown }).error)
        : response.statusText
    throw new ApiError(message, response.status)
  }
  return response.blob()
}

const rowsPath = (t: TableInfo) =>
  `/api/tables/${encodeURIComponent(t.schema)}/${encodeURIComponent(t.name)}/rows`

const exportPath = (t: TableInfo) =>
  `/api/tables/${encodeURIComponent(t.schema)}/${encodeURIComponent(t.name)}/export`

export type RowsQuery = {
  limit: number
  offset: number
  order?: string
  dir?: "asc" | "desc"
  filters: Filter[]
}

function filterParams(q: Pick<RowsQuery, "order" | "dir" | "filters">): URLSearchParams {
  const params = new URLSearchParams({ filters: JSON.stringify(q.filters) })
  if (q.order) {
    params.set("order", q.order)
    params.set("dir", q.dir ?? "asc")
  }
  return params
}

export const api = {
  meta: () => request<Meta>("GET", "/api/meta"),
  tables: () => request<{ tables: TableInfo[] }>("GET", "/api/tables"),
  relationships: () =>
    request<{ tables: DiagramNode[]; relationships: DiagramEdge[] }>(
      "GET",
      "/api/schema/relationships"
    ),
  rows: (table: TableInfo, q: RowsQuery) => {
    const params = filterParams(q)
    params.set("limit", String(q.limit))
    params.set("offset", String(q.offset))
    return request<{ total: number; rows: Row[] }>("GET", `${rowsPath(table)}?${params}`)
  },
  export: (
    table: TableInfo,
    q: Pick<RowsQuery, "order" | "dir" | "filters">,
    format: "csv" | "json"
  ) => {
    const params = filterParams(q)
    params.set("format", format)
    return requestBlob(`${exportPath(table)}?${params}`)
  },
  insert: (table: TableInfo, values: Row) =>
    request<{ row: Row }>("POST", rowsPath(table), { values }),
  update: (table: TableInfo, key: Row, values: Row) =>
    request<{ row: Row }>("PATCH", rowsPath(table), { key, values }),
  remove: (table: TableInfo, keys: Row[]) =>
    request<{ deleted: number }>("DELETE", rowsPath(table), { keys }),
  sql: (sql: string) => request<SqlBatch>("POST", "/api/sql", { sql }),
  ddlPreview: (op: DdlOperation) => request<DdlPreview>("POST", "/api/ddl/preview", op),
  ddlApply: (op: DdlOperation) => request<DdlResult>("POST", "/api/ddl/apply", op),
  history: () => request<{ history: HistoryEntry[] }>("GET", "/api/queries/history"),
  clearHistory: () => request<{ cleared: boolean }>("DELETE", "/api/queries/history"),
  saved: () => request<{ saved: SavedQuery[] }>("GET", "/api/queries/saved"),
  saveQuery: (name: string, sql: string) =>
    request<{ name: string }>("PUT", `/api/queries/saved/${encodeURIComponent(name)}`, { sql }),
  deleteQuery: (name: string) =>
    request<{ deleted: string }>("DELETE", `/api/queries/saved/${encodeURIComponent(name)}`),
}

// S6-1: not "schema.name": public."x.y" and "public.x".y would share it.
export const tableKey = (t: TableInfo) => JSON.stringify([t.schema, t.name])

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "NULL"
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

/** Text the user edits for a cell: JSON for objects/arrays, plain text otherwise. */
export function editText(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

function pgArrayLiteral(items: unknown[]): string {
  const quoted = items.map((item) => {
    if (item === null) return "NULL"
    if (Array.isArray(item)) return pgArrayLiteral(item)
    const text = typeof item === "object" ? JSON.stringify(item) : String(item)
    return `"${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`
  })
  return `{${quoted.join(",")}}`
}

/** Convert what the user typed into the text form the server casts to the column type. */
export function toDbText(column: Column, text: string): string {
  if (column.type.endsWith("[]")) {
    const trimmed = text.trim()
    if (trimmed.startsWith("[")) {
      // Not JSON: the array's own text, "[0:1]={5,6}" with its bounds (S6-4).
      let parsed: unknown
      try {
        parsed = JSON.parse(trimmed)
      } catch {
        return text
      }
      if (Array.isArray(parsed)) return pgArrayLiteral(parsed)
    }
  }
  return text
}
