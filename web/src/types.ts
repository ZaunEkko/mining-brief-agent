export type ServerName = 'news' | 'pdf' | 'price'

export interface Meta {
  llm: string | null
  offline: boolean
  transport: 'http' | 'stdio'
  examples: { brief: string[]; ask: string[] }
}

export interface ToolInfo {
  name: string
  description: string | null
  input_schema: JsonSchema
  output_schema: JsonSchema | null
}

export interface ServerInfo {
  server: ServerName
  label: string
  connected: boolean
  error: string | null
  tools: ToolInfo[]
}

export interface JsonSchema {
  type?: string | string[]
  description?: string
  default?: unknown
  properties?: Record<string, JsonSchema>
  required?: string[]
  anyOf?: JsonSchema[]
  minimum?: number
  maximum?: number
}

/** One MCP tool call as shown in the core log. */
export interface CallEntry {
  kind: 'call'
  id: string
  server: ServerName
  tool: string
  args: Record<string, unknown>
  status: 'running' | 'ok' | 'error'
  /** ms after the run started */
  at: number
  ms?: number
  summary?: string
}

/** Planner output, LLM interim text and warnings: margin notes between calls. */
export interface NoteEntry {
  kind: 'note'
  tone: 'plan' | 'text' | 'warning'
  text: string
  at: number
}

/** One LLM round: deciding which tools to call, or writing the answer / summary. */
export interface LlmEntry {
  kind: 'llm'
  id: string
  model: string
  phase: 'decide' | 'compose'
  status: 'running' | 'ok' | 'error'
  at: number
  ms?: number
  /** tool calls requested in this round (decide phase) */
  calls?: number
}

export type LogEntry = CallEntry | LlmEntry | NoteEntry

export interface BriefDone {
  markdown: string
  generator: string
}

export interface AskDone {
  markdown: string
  model: string
  calls: { name: string; arguments: Record<string, unknown> }[]
  unverified_urls: string[]
  stopped_early: boolean
}
