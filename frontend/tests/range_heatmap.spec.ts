import { expect, test } from '@playwright/test'

test('事件聚合 → 行为热力：区间覆盖、可比格子与版本一致', async ({ page }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  // 1. 09:40-10:20：九点只保留整桶，缺区间原始记录，不能拿整小时总数凑数。
  await page.goto('/event_aggregate')
  await page.getByTestId('range-config').selectOption('EA-004')
  await page.getByTestId('range-start').fill('09:40')
  await page.getByTestId('range-end').fill('10:20')
  await page.getByTestId('run-analysis').click()

  const nine = page.getByTestId('coverage-2026-11-15T09:00')
  const ten = page.getByTestId('coverage-2026-11-15T10:00')
  await expect(nine).toContainText('missing_detail')
  await expect(nine).toContainText('不可用')
  await expect(ten).toContainText('分钟明细')

  // 2. 整点查询 09:00-10:00 仍走历史整桶统计。
  await page.getByTestId('range-start').fill('09:00')
  await page.getByTestId('range-end').fill('10:00')
  await page.getByTestId('run-analysis').click()
  await expect(page.getByTestId('coverage-2026-11-15T09:00')).toContainText('整桶统计')
  await expect(page.getByTestId('coverage-2026-11-15T09:00')).toContainText('complete')

  // 3. 热力：九点没有格子；所有格子都标注实际覆盖时段，且只用一个版本。
  await page.goto('/behavior_heatmap')
  await page.getByTestId('heatmap-config').selectOption('BH-001')
  await page.getByTestId('heatmap-start').fill('09:40')
  await page.getByTestId('heatmap-end').fill('10:20')
  await page.getByTestId('run-analysis').click()

  const versionLabel = page.getByTestId('heatmap-version-label')
  await expect(versionLabel).toContainText('EA-004@')
  await expect(page.getByTestId('heatmap-cell-2026-11-15T09:00')).toHaveCount(0)

  const tenCells = page.getByTestId('heatmap-cell-2026-11-15T10:00')
  await expect(tenCells.first()).toBeVisible()
  await expect(tenCells.first()).toContainText('10:00 - 10:20')

  // 4. 补录十点后重查原区间：十点转为完整，九点依旧缺覆盖。
  await page.goto('/event_aggregate')
  await page.getByTestId('range-config').selectOption('EA-004')
  await page.getByTestId('range-start').fill('09:40')
  await page.getByTestId('range-end').fill('10:20')
  await page.getByTestId('run-analysis').click()
  const backfill = page.getByTestId('backfill-2026-11-15T10:00')
  if (await backfill.count()) {
    await backfill.click()
  }
  await expect(page.getByTestId('coverage-2026-11-15T10:00')).toContainText('complete')
  await expect(page.getByTestId('coverage-2026-11-15T09:00')).toContainText('missing_detail')

  // 5. 补录后的热力：只由可比较格子决定色阶，且仍锁定单一版本。
  await page.goto('/behavior_heatmap')
  await page.getByTestId('heatmap-config').selectOption('BH-001')
  await page.getByTestId('heatmap-start').fill('09:40')
  await page.getByTestId('heatmap-end').fill('10:20')
  await page.getByTestId('run-analysis').click()
  await expect(page.getByTestId('heatmap-version-label')).toContainText('EA-004@')
  await expect(page.locator('.heatmap-meta')).toContainText('仅可比较格子')
  await expect(page.locator('.heatmap-meta')).toContainText('2026-11-15T10:00')

  expect(browserErrors).toEqual([])
})
