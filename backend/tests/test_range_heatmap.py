"""事件聚合按时间范围查询 + 行为热力按版本重算的集成测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.behavior_heatmap import generate_heatmap
from app.services.event_aggregate import (
    aggregate_events,
    backfill_hour,
    get_aggregate_version,
    query_aggregate_detail,
)
from app.store import store

CONFIG = "EA-004"
HEATMAP = "BH-001"


class RangeQueryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        # 重置补录状态，避免用例之间互相影响。
        store._artifacts.pop("event_aggregate_ingest", None)
        aggregate_events(CONFIG)


class TestAggregateRangeQuery(RangeQueryTestBase):
    def test_partial_range_does_not_use_whole_hour_total(self) -> None:
        """09:40-10:20：九点只有整桶，不能拿整小时总数当区间值。"""
        result = query_aggregate_detail(CONFIG, start="09:40", end="10:20")

        buckets = {bucket["bucket"][11:13]: bucket for bucket in result["buckets"]}
        nine, ten = buckets["09"], buckets["10"]

        self.assertEqual(nine["coverage_status"], "missing_detail")
        self.assertEqual(nine["source"], "unavailable")
        self.assertFalse(nine["exact"])
        self.assertFalse(nine["comparable"])
        self.assertEqual(nine["rows"], [], "不允许回退到整小时总数")

        # 十点保留分钟明细，但仍在补录：结果只能当作下界。
        self.assertEqual(ten["source"], "minute_detail")
        self.assertEqual(ten["coverage_status"], "partial")
        self.assertFalse(ten["exact"])
        self.assertFalse(ten["comparable"])
        self.assertGreater(ten["coverage_ratio"], 0)
        self.assertLess(ten["coverage_ratio"], 1)
        self.assertGreater(len(ten["rows"]), 0)

        self.assertEqual(result["comparable_buckets"], [])
        self.assertEqual(len(result["incomparable_buckets"]), 2)

    def test_whole_hour_query_uses_bucket_stats(self) -> None:
        """整点查询仍可使用历史整桶统计。"""
        result = query_aggregate_detail(CONFIG, start="09:00", end="10:00")
        bucket = result["buckets"][0]
        self.assertEqual(bucket["source"], "whole_bucket")
        self.assertEqual(bucket["coverage_status"], "complete")
        self.assertTrue(bucket["exact"])
        self.assertTrue(bucket["comparable"])
        self.assertGreater(len(bucket["rows"]), 0)
        self.assertEqual(result["comparable_buckets"], ["2026-11-15T09:00"])

    def test_incomplete_whole_hour_is_not_exact(self) -> None:
        """十点仍在补录，整点查询也不能直接当精确值。"""
        result = query_aggregate_detail(CONFIG, start="10:00", end="11:00")
        bucket = result["buckets"][0]
        self.assertEqual(bucket["source"], "whole_bucket")
        self.assertEqual(bucket["coverage_status"], "partial")
        self.assertFalse(bucket["exact"])
        self.assertFalse(bucket["comparable"])
        self.assertIn("补录", bucket["note"])

    def test_backfill_makes_partial_bucket_complete(self) -> None:
        """补录十点迟到事件后，同一区间的十点桶变为精确。"""
        backfill_hour(CONFIG, 10)
        result = query_aggregate_detail(CONFIG, start="09:40", end="10:20")
        buckets = {bucket["bucket"][11:13]: bucket for bucket in result["buckets"]}
        self.assertTrue(buckets["10"]["exact"])
        self.assertTrue(buckets["10"]["comparable"])
        self.assertEqual(buckets["10"]["coverage_ratio"], 1.0)
        # 九点依然只有整桶，区间部分时段仍不可用。
        self.assertEqual(buckets["09"]["coverage_status"], "missing_detail")
        self.assertEqual(result["comparable_buckets"], ["2026-11-15T10:00"])

    def test_backfilled_values_are_not_lower_than_before(self) -> None:
        before = query_aggregate_detail(CONFIG, start="09:40", end="10:20")
        before_total = sum(
            row["agg_value"]
            for bucket in before["buckets"] for row in bucket["rows"]
        )
        backfill_hour(CONFIG, 10)
        after = query_aggregate_detail(CONFIG, start="09:40", end="10:20")
        after_total = sum(
            row["agg_value"]
            for bucket in after["buckets"] for row in bucket["rows"]
        )
        self.assertGreater(after_total, before_total)

    def test_invalid_range_rejected(self) -> None:
        with self.assertRaises(ValueError):
            query_aggregate_detail(CONFIG, start="10:20", end="09:40")
        with self.assertRaises(ValueError):
            query_aggregate_detail(CONFIG, start="", end="10:20")

    def test_detail_queried_by_pinned_version(self) -> None:
        """查询与补数交错时，指定版本仍返回旧快照。"""
        first = query_aggregate_detail(CONFIG, start="09:40", end="10:20")
        version = first["version"]
        backfill_hour(CONFIG, 10)
        latest = query_aggregate_detail(CONFIG, start="09:40", end="10:20")
        self.assertNotEqual(version, latest["version"])

        pinned = query_aggregate_detail(
            CONFIG, start="09:40", end="10:20", version=version
        )
        self.assertEqual(pinned["version"], version)
        self.assertEqual(pinned["buckets"], first["buckets"])
        self.assertEqual(pinned["coverage"], first["coverage"])


class TestHeatmapVersioning(RangeQueryTestBase):
    def test_heatmap_uses_single_aggregate_version(self) -> None:
        """查询与补数交错：一张热力图只使用同一聚合版本。"""
        pre = generate_heatmap(HEATMAP, start="09:40", end="10:20")
        first_version = pre["aggregate_version"]

        # 生成后立刻补数：新版本落地，但已生成的热力结果不能被改写。
        backfill_hour(CONFIG, 10)

        post = generate_heatmap(HEATMAP, start="09:40", end="10:20")
        self.assertNotEqual(post["aggregate_version"], first_version)

        pinned = generate_heatmap(
            HEATMAP, start="09:40", end="10:20", version=first_version
        )
        self.assertEqual(pinned["aggregate_version"], first_version)
        self.assertEqual(pinned["source_versions"], [first_version])
        self.assertEqual(pinned["cell_index"], pre["cell_index"])
        self.assertEqual(pinned["coverage"], pre["coverage"])

    def test_color_scale_ignores_incomplete_cells(self) -> None:
        """补录后：只有十点可比较，九点格子被排除在色阶与比较之外。"""
        backfill_hour(CONFIG, 10)
        heatmap = generate_heatmap(HEATMAP, start="09:40", end="10:20")

        self.assertEqual(heatmap["comparable_buckets"], ["2026-11-15T10:00"])
        self.assertEqual(heatmap["incomparable_buckets"], ["2026-11-15T09:00"])
        self.assertEqual(heatmap["scale_source"], "comparable_cells_only")

        ten_cells = [
            cell for cell in heatmap["cell_index"]
            if cell["time_slot"] == "2026-11-15T10:00"
        ]
        self.assertTrue(ten_cells)
        self.assertTrue(all(cell["in_color_scale"] for cell in ten_cells))
        self.assertEqual(
            heatmap["scale_min"], min(cell["value"] for cell in ten_cells)
        )
        self.assertEqual(
            heatmap["scale_max"], max(cell["value"] for cell in ten_cells)
        )

        # 九点没有格子，因为它只有整桶统计，区间时段缺原始记录。
        self.assertFalse(
            [cell for cell in heatmap["cell_index"] if cell["time_slot"] == "2026-11-15T09:00"]
        )
        self.assertEqual(
            [item["bucket"] for item in heatmap["unavailable_buckets"]],
            ["2026-11-15T09:00"],
        )
        self.assertIn("不能用整小时总数", heatmap["unavailable_buckets"][0]["reason"])
        # 十点是唯一的可比较桶，无法两两比较。
        self.assertEqual(heatmap["comparisons"], [])

    def test_before_backfill_nothing_is_comparable(self) -> None:
        heatmap = generate_heatmap(HEATMAP, start="09:40", end="10:20")
        self.assertEqual(heatmap["scale_source"], "no_comparable_cells")
        self.assertEqual(heatmap["scale_min"], 0)
        self.assertEqual(heatmap["scale_max"], 0)
        self.assertTrue(
            all(not cell["in_color_scale"] for cell in heatmap["cell_index"])
        )
        self.assertTrue(heatmap["excluded_cells"])

    def test_whole_hour_heatmap_uses_bucket_stats(self) -> None:
        heatmap = generate_heatmap(HEATMAP, start="09:00", end="10:00")
        self.assertEqual(heatmap["comparable_buckets"], ["2026-11-15T09:00"])
        self.assertEqual(
            {cell["source"] for cell in heatmap["cell_index"]}, {"whole_bucket"}
        )
        self.assertTrue(all(cell["exact"] for cell in heatmap["cell_index"]))

    def test_cross_hour_heatmap_excludes_pending_bucket(self) -> None:
        heatmap = generate_heatmap(HEATMAP, start="09:00", end="11:00")
        self.assertIn("2026-11-15T09:00", heatmap["comparable_buckets"])
        self.assertIn("2026-11-15T10:00", heatmap["incomparable_buckets"])
        self.assertEqual(heatmap["comparisons"], [])

    def test_heatmap_pins_versions_per_config(self) -> None:
        heatmap = generate_heatmap(HEATMAP)
        self.assertEqual(heatmap["source_versions"], [heatmap["aggregate_version"]])
        self.assertEqual(heatmap["source_aggregate"], CONFIG)
        # 默认时间范围覆盖整个数据日，逐小时都关联到实际时间桶。
        self.assertEqual(len(heatmap["time_window"]["covered_buckets"]), 24)


class TestVersionLedger(RangeQueryTestBase):
    def test_versions_are_immutable(self) -> None:
        first = get_aggregate_version(CONFIG)
        stored = get_aggregate_version(CONFIG, first["version"])
        snapshot_rows = [dict(row) for row in stored["result_rows"]]
        backfill_hour(CONFIG, 10)
        again = get_aggregate_version(CONFIG, first["version"])
        self.assertEqual(again["result_rows"], snapshot_rows)
        self.assertEqual(again["version"], first["version"])
        self.assertNotEqual(get_aggregate_version(CONFIG)["version"], first["version"])

    def test_version_must_match_config(self) -> None:
        version = get_aggregate_version(CONFIG)["version"]
        with self.assertRaises(ValueError):
            get_aggregate_version("EA-001", version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
