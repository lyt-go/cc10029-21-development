<template>
  <section class="panel" data-testid="field-mapping-panel">
    <header class="panel-head">
      <div>
        <h3>{{ mode === 'target' ? '目标端入库领用核对' : '字段映射批次与拒收明细' }}</h3>
        <p class="page-desc">
          缺列、显式空、转换异常分别生成拒收回执；整批完成后目标端才能领用可入库数据。
        </p>
      </div>
      <button class="btn ghost" type="button" data-testid="mapping-refresh" @click="refresh">
        刷新批次
      </button>
    </header>

    <div class="mapping-toolbar">
      <label class="filter-item">
        <span>批次编号</span>
        <input v-model="batchCode" data-testid="mapping-batch-code" placeholder="例如 BATCH-API" />
      </label>
      <button class="btn" type="button" data-testid="mapping-load" @click="loadBatch">查询批次</button>
      <button
        v-if="mode === 'target'"
        class="btn primary"
        type="button"
        data-testid="mapping-claim"
        @click="claim"
      >
        发起入库领用
      </button>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <table v-if="batches.length" class="data-table">
      <thead>
        <tr><th>批次编号</th><th>源端编号</th><th>批次状态</th><th>契约版本</th><th>已映射</th><th>未完成键</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="item in batches"
          :key="item.batch_code"
          :data-testid="`mapping-batch-${item.batch_code}`"
        >
          <td>
            <button class="link" type="button" @click="selectBatch(item.batch_code)">
              {{ item.batch_code }}
            </button>
          </td>
          <td>{{ item.source_code || '—' }}</td>
          <td>{{ item.status }}</td>
          <td>{{ item.contract_version || '—' }}</td>
          <td>{{ item.mapped_count }}</td>
          <td>{{ item.pending_keys.join('、') || '—' }}</td>
        </tr>
      </tbody>
    </table>

    <div v-if="claimResult" class="claim-summary" data-testid="mapping-claim-summary">
      <p>
        领用结论：<strong>{{ claimResult.status }}</strong>
        <span v-if="claimResult.can_ingest">，可入库 {{ claimResult.available }} 条</span>
        <span v-else>，已通过校验 {{ claimResult.validated_count ?? 0 }} 条尚未承诺交付</span>
      </p>
      <p v-if="claimResult.rejected_by_category && Object.keys(claimResult.rejected_by_category).length">
        拒收分类：
        <span v-for="(count, category) in claimResult.rejected_by_category" :key="category">
          {{ category }} {{ count }} 条；
        </span>
      </p>
      <p v-if="claimResult.pending_keys && claimResult.pending_keys.length">
        未完成键：{{ claimResult.pending_keys.join('、') }}
      </p>
    </div>

    <table v-if="rejections.length" class="data-table">
      <thead>
        <tr>
          <th>拒收编号</th><th>记录主键</th><th>字段类别</th><th>源字段</th>
          <th>契约版本</th><th>拒收原因</th><th>补交状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rejections"
          :key="row.拒收编号"
          :data-testid="`mapping-reject-${row.记录主键}`"
        >
          <td>{{ row.拒收编号 }}</td>
          <td>{{ row.记录主键 }}</td>
          <td>{{ row.字段类别 }}</td>
          <td>{{ row.源字段 || '—' }}</td>
          <td>{{ row.契约版本 }}</td>
          <td>{{ row.拒收原因 }}</td>
          <td>{{ row.补交状态 }}{{ row.补交契约版本 ? `（${row.补交契约版本}）` : '' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  claimIngest,
  listFieldRejections,
  listMappingBatches,
} from '@/api/backend-service'
import type { FieldRejection, IngestClaim, MappingBatch } from '@/data/types'

defineProps<{ mode: 'contract' | 'target' }>()

const batches = ref<MappingBatch[]>([])
const rejections = ref<FieldRejection[]>([])
const claimResult = ref<IngestClaim | null>(null)
const batchCode = ref('')
const errorMessage = ref('')

async function refresh() {
  errorMessage.value = ''
  try {
    batches.value = await listMappingBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次读取失败'
  }
}

async function loadBatch() {
  const code = batchCode.value.trim()
  if (!code) {
    errorMessage.value = '请先填写批次编号'
    return
  }
  errorMessage.value = ''
  try {
    rejections.value = await listFieldRejections(code)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '拒收明细读取失败'
  }
}

async function claim() {
  const code = batchCode.value.trim()
  if (!code) {
    errorMessage.value = '请先填写批次编号'
    return
  }
  errorMessage.value = ''
  try {
    claimResult.value = await claimIngest(code)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领用请求失败'
  }
}

function selectBatch(code: string) {
  batchCode.value = code
  void loadBatch()
}

onMounted(refresh)
</script>
