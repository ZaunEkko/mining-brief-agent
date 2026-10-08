<script setup lang="ts">
/** Direct access to every MCP tool: schema on the left, call and raw result on the right. */
import { computed, ref, watch } from 'vue'
import { createLatest } from '../lib/latest'
import { errorText } from '../lib/sse'
import type { JsonSchema, ServerInfo, ToolInfo } from '../types'

const props = defineProps<{ servers: ServerInfo[] | null; loadError: string | null }>()

// Arguments that produce a useful result out of the box.
const EXAMPLES: Record<string, Record<string, unknown>> = {
  search: { query: 'PLS lithium', days: 7, limit: 5 },
  fetch_article: { url: 'https://www.mining.com/web/zimbabwes-lithium-export-earnings-surge-on-higher-spodumene-prices/', max_chars: 2000 },
  extract_resources: {
    pdf_url: 'https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/2924-02955435-6A1268075',
  },
  get_price: { commodity: 'copper' },
  get_trend: { commodity: 'lithium_carbonate', days: 30 },
}

const selected = ref<{ server: string; tool: ToolInfo } | null>(null)
const args = ref('{}')
const output = ref<string | null>(null)
const outcome = ref<'ok' | 'error' | null>(null)
const ms = ref<number | null>(null)
const busy = ref(false)
// A response that arrives after the user picked another tool must not overwrite it.
const latest = createLatest()

const params = computed(() => {
  const schema = selected.value?.tool.input_schema
  const required = new Set(schema?.required ?? [])
  return Object.entries(schema?.properties ?? {}).map(([name, prop]) => ({
    name,
    type: typeOf(prop),
    required: required.has(name),
    description: prop.description ?? '',
    default: prop.default,
  }))
})

watch(
  () => props.servers,
  (servers) => {
    const first = servers?.find((s) => s.connected && s.tools.length)
    if (first && !selected.value) select(first.server, first.tools[0]!)
  },
  { immediate: true },
)

function typeOf(prop: JsonSchema): string {
  if (prop.anyOf) return prop.anyOf.map(typeOf).join(' | ')
  return Array.isArray(prop.type) ? prop.type.join(' | ') : (prop.type ?? 'any')
}

function select(server: string, tool: ToolInfo): void {
  latest.invalidate()
  busy.value = false
  selected.value = { server, tool }
  args.value = JSON.stringify(EXAMPLES[tool.name] ?? {}, null, 2)
  output.value = null
  outcome.value = null
  ms.value = null
}

async function call(): Promise<void> {
  if (!selected.value) return
  let parsed: unknown
  try {
    parsed = JSON.parse(args.value)
  } catch (e) {
    outcome.value = 'error'
    output.value = `参数不是合法 JSON：${e instanceof Error ? e.message : String(e)}`
    return
  }
  const token = latest.next()
  busy.value = true
  output.value = null
  try {
    const response = await fetch(`/api/tools/${selected.value.server}/${selected.value.tool.name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ arguments: parsed }),
    })
    if (!response.ok) throw new Error(await errorText(response))
    const data = (await response.json()) as { ok: boolean; result?: unknown; error?: string; ms: number }
    if (!latest.isCurrent(token)) return
    outcome.value = data.ok ? 'ok' : 'error'
    ms.value = data.ms
    output.value = data.ok ? JSON.stringify(data.result, null, 2) : (data.error ?? '未知错误')
  } catch (e) {
    if (!latest.isCurrent(token)) return
    outcome.value = 'error'
    output.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (latest.isCurrent(token)) busy.value = false
  }
}
</script>

<template>
  <div class="tools">
    <p v-if="loadError" class="error-box">无法读取工具列表：{{ loadError }}</p>
    <p v-else-if="!servers" class="loading">正在连接三个 MCP server…</p>

    <div v-else class="grid">
      <nav class="catalogue" aria-label="MCP 工具">
        <section v-for="s in servers" :key="s.server" :class="`server-${s.server}`">
          <h3>
            <i class="dot" :class="{ down: !s.connected }" />
            <code>{{ s.label }}</code>
          </h3>
          <p v-if="!s.connected" class="down-note">未连接：{{ s.error }}</p>
          <button
            v-for="t in s.tools"
            :key="t.name"
            type="button"
            class="tool"
            :class="{ active: selected?.tool.name === t.name }"
            @click="select(s.server, t)"
          >
            <code>{{ t.name }}</code>
            <span>{{ t.description }}</span>
          </button>
        </section>
      </nav>

      <section v-if="selected" class="detail">
        <div class="eyebrow">{{ selected.server }} · 输入参数</div>
        <table class="params">
          <tbody>
            <tr v-for="p in params" :key="p.name">
              <td><code>{{ p.name }}</code><span v-if="p.required" class="req">必填</span></td>
              <td class="type">{{ p.type }}</td>
              <td>{{ p.description }}<span v-if="p.default !== undefined" class="def"> 默认 {{ p.default }}</span></td>
            </tr>
          </tbody>
        </table>

        <label class="eyebrow" for="tool-args">arguments（JSON）</label>
        <textarea id="tool-args" v-model="args" class="field mono" rows="6" spellcheck="false" />
        <div class="run">
          <button class="btn" type="button" :disabled="busy" @click="call">{{ busy ? '调用中…' : `调用 ${selected.tool.name}` }}</button>
          <span v-if="ms !== null" class="ms" :class="outcome">{{ outcome === 'ok' ? '成功' : '失败' }} · {{ ms }} ms</span>
        </div>
        <pre v-if="output" class="output" :class="outcome">{{ output }}</pre>
      </section>
    </div>
  </div>
</template>

<style scoped>
.loading {
  color: var(--ink-3);
}

.grid {
  display: grid;
  grid-template-columns: minmax(240px, 320px) 1fr;
  gap: clamp(20px, 3vw, 40px);
  align-items: start;
}

.catalogue section {
  margin-bottom: 18px;
}

.catalogue h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 14px;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  background: var(--lith);
}

.dot.down {
  background: var(--hematite);
}

.down-note {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--hematite);
}

.tool {
  display: grid;
  gap: 2px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-left: 3px solid var(--lith);
  border-radius: 0 var(--radius) var(--radius) 0;
  background: none;
  text-align: left;
  cursor: pointer;
}

.tool span {
  font-size: 12px;
  color: var(--ink-3);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.tool.active,
.tool:hover {
  background: var(--surface);
  border-color: var(--rule);
  border-left-color: var(--lith);
}

.detail {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.params {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.params td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}

.params .type {
  font: 12px var(--font-data);
  color: var(--ink-3);
  white-space: nowrap;
}

.req {
  margin-left: 6px;
  font-size: 11px;
  color: var(--hematite);
}

.def {
  color: var(--ink-3);
}

.mono {
  font: 13px/1.5 var(--font-data);
}

.run {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ms {
  font: 12px var(--font-data);
}

.ms.ok {
  color: var(--jade);
}

.ms.error {
  color: var(--hematite);
}

.output {
  margin: 0;
  max-height: 60vh;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--surface);
  font: 12px/1.5 var(--font-data);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.output.error {
  border-color: var(--hematite);
  color: var(--hematite);
}

@media (max-width: 820px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
