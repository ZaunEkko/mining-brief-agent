<script setup lang="ts">
import { onMounted, ref } from 'vue'
import RunView from './components/RunView.vue'
import ToolsView from './components/ToolsView.vue'
import { errorText } from './lib/sse'
import type { Meta, ServerInfo } from './types'

type Tab = 'brief' | 'ask' | 'tools'
const TABS: { id: Tab; label: string }[] = [
  { id: 'brief', label: '日报' },
  { id: 'ask', label: '问答' },
  { id: 'tools', label: 'MCP 工具' },
]

const tab = ref<Tab>('brief')
const meta = ref<Meta | null>(null)
const servers = ref<ServerInfo[] | null>(null)
const loadError = ref<string | null>(null)

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(await errorText(response))
  return (await response.json()) as T
}

onMounted(async () => {
  try {
    meta.value = await getJson<Meta>('/api/meta')
    servers.value = await getJson<ServerInfo[]>('/api/tools')
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <div class="wordmark">
        <span class="mark" aria-hidden="true"><i class="server-news" /><i class="server-pdf" /><i class="server-price" /></span>
        <div>
          <h1>矿权日报</h1>
          <p class="eyebrow">MINING BRIEF · MODEL CONTEXT PROTOCOL</p>
        </div>
      </div>

      <nav class="tabs" role="tablist">
        <button
          v-for="t in TABS"
          :key="t.id"
          role="tab"
          :aria-selected="tab === t.id"
          :class="{ active: tab === t.id }"
          @click="tab = t.id"
        >
          {{ t.label }}
        </button>
      </nav>

      <div class="status">
        <span v-for="s in servers ?? []" :key="s.server" class="server" :class="[`server-${s.server}`, { down: !s.connected }]" :title="s.error ?? '已连接'">
          <i />{{ s.label }}
        </span>
        <span class="llm">{{ meta ? (meta.llm ?? '未配置 LLM') : '…' }}<template v-if="meta?.offline"> · 离线示例数据</template></span>
      </div>
    </header>

    <p v-if="loadError" class="error-box">无法连接后端 API：{{ loadError }}。确认已运行 <code>mining-brief-web</code>。</p>

    <!-- v-show keeps each workbench's last run when switching tabs. -->
    <RunView v-show="tab === 'brief'" mode="brief" :meta="meta" :key="`brief-${meta ? 1 : 0}`" />
    <RunView v-show="tab === 'ask'" mode="ask" :meta="meta" :key="`ask-${meta ? 1 : 0}`" />
    <ToolsView v-show="tab === 'tools'" :servers="servers" :load-error="loadError" />
  </div>
</template>

<style scoped>
.shell {
  max-width: 1320px;
  margin: 0 auto;
  padding: 20px var(--gutter) 64px;
}

.masthead {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: end;
  gap: 24px;
  padding-bottom: 14px;
  margin-bottom: 24px;
  border-bottom: 2px solid var(--ink);
}

.wordmark {
  display: flex;
  align-items: center;
  gap: 14px;
}

/* Three stacked core plugs: the servers, in lithology colours. */
.mark {
  display: grid;
  width: 14px;
}

.mark i {
  display: block;
  height: 13px;
  background: var(--lith);
}

.mark i:first-child {
  border-radius: 3px 3px 0 0;
}

.mark i:last-child {
  border-radius: 0 0 3px 3px;
}

h1 {
  margin: 0;
  font: 800 30px/1 var(--font-display);
  font-stretch: 68%;
  letter-spacing: 0.02em;
}

.wordmark .eyebrow {
  margin: 6px 0 0;
}

.tabs {
  display: flex;
  gap: 4px;
  justify-self: center;
}

.tabs button {
  padding: 8px 16px;
  border: none;
  border-bottom: 3px solid transparent;
  background: none;
  font-weight: 600;
  color: var(--ink-3);
  cursor: pointer;
}

.tabs button.active {
  color: var(--ink);
  border-bottom-color: var(--ink);
}

.status {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px 14px;
  font: 11.5px var(--font-data);
  color: var(--ink-3);
}

.server i {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 5px;
  border-radius: 2px;
  background: var(--lith);
}

.server.down {
  color: var(--hematite);
}

.server.down i {
  background: var(--hematite);
}

.llm {
  color: var(--ink-2);
}

@media (max-width: 960px) {
  .masthead {
    grid-template-columns: 1fr;
    align-items: start;
    gap: 12px;
  }

  .tabs {
    justify-self: start;
  }

  .status {
    justify-content: flex-start;
  }
}
</style>
