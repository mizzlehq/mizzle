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
      Authorization: `Bearer ${token}`,
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

const rowsPath = (t: TableInfo) =>
  `/api/tables/${encodeURIComponent(t.schema)}/${encodeURIComponent(t.name)}/rows`

export type RowsQuery = {
  limit: number
  offset: number
  order?: string
  dir?: "asc" | "desc"
  filters: Filter[]
}

export const api = {
  meta: () => request<Meta>("GET", "/api/meta"),
  tables: () => request<{ tables: TableInfo[] }>("GET", "/api/tables"),
  rows: (table: TableInfo, q: RowsQuery) => {
    const params = new URLSearchParams({
      limit: String(q.limit),
      offset: String(q.offset),
      filters: JSON.stringify(q.filters),
    })
    if (q.order) {
      params.set("order", q.order)
      params.set("dir", q.dir ?? "asc")
    }
    return request<{ total: number; rows: Row[] }>("GET", `${rowsPath(table)}?${params}`)
  },
  insert: (table: TableInfo, values: Row) =>
    request<{ row: Row }>("POST", rowsPath(table), { values }),
  update: (table: TableInfo, key: Row, values: Row) =>
    request<{ row: Row }>("PATCH", rowsPath(table), { key, values }),
  remove: (table: TableInfo, keys: Row[]) =>
    request<{ deleted: number }>("DELETE", rowsPath(table), { keys }),
  sql: (sql: string) => request<SqlResult>("POST", "/api/sql", { sql }),
}

export const tableKey = (t: TableInfo) => `${t.schema}.${t.name}`

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
      const parsed: unknown = JSON.parse(trimmed)
      if (Array.isArray(parsed)) return pgArrayLiteral(parsed)
    }
  }
  return text
}
