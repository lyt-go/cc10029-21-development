import { fetchJson, request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  FieldRejection,
  IngestClaim,
  MappingBatch,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// -- 字段映射批次 --

export async function listMappingBatches(): Promise<MappingBatch[]> {
  const payload = await fetchJson<{ items: MappingBatch[] }>('/api/field-mapping/batches')
  return payload.items
}

export async function ensureContracts(
  specs: { source_field: string; target_field: string; data_type?: string }[],
): Promise<EntryRow[]> {
  const response = await request('/api/field-mapping/contracts/ensure', {
    method: 'POST',
    body: JSON.stringify({ contracts: specs }),
  })
  if (!response.ok) throw new Error('字段契约准备失败')
  const payload = (await response.json()) as { items: EntryRow[] }
  return payload.items
}

export async function applyMappingBatch(
  sourceCode: string,
  batchCode: string,
  records: Record<string, unknown>[],
  contractCodes?: string[],
): Promise<Record<string, unknown>> {
  const response = await request('/api/field-mapping/apply', {
    method: 'POST',
    body: JSON.stringify({
      source_code: sourceCode,
      batch_code: batchCode,
      records,
      contract_codes: contractCodes,
    }),
  })
  if (!response.ok) throw new Error('字段映射批次执行失败')
  return await response.json()
}

export async function listFieldRejections(batchCode: string): Promise<FieldRejection[]> {
  const payload = await fetchJson<{ items: FieldRejection[] }>(
    `/api/field-mapping/${encodeURIComponent(batchCode)}/rejections`,
  )
  return payload.items
}

export async function claimIngest(batchCode: string): Promise<IngestClaim> {
  const response = await request(`/api/field-mapping/${encodeURIComponent(batchCode)}/claim`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('入库领用请求失败')
  return await response.json()
}
