"""行为热力服务：读取事件聚合明细，重算格值与色阶。

热力图不再自己造数，而是消费事件聚合的范围查询结果：

* 同一张热力图只读取一个聚合版本（生成开始时钉住版本号），即使查询与补数
  交错，也不会出现一部分格子来自旧版本、一部分来自新版本；
* 每个格子只使用覆盖完整的时间桶，并记录该格实际覆盖的时间桶；
* 色阶范围只由可比较的格子决定，缺覆盖的格子不参与色阶，也不参与比较。
"""
from __future__ import annotations

from typing import Any

from app.services.event_aggregate import get_aggregate_version, query_aggregate_detail
from app.store import store

# 简化色阶：低 → 高，按 level 取色，实际项目里会换成配置的色板。
_COLOR_STOPS = ["#f1f5f9", "#bfdbfe", "#60a5fa", "#2563eb", "#1e3a8a"]
_ADDITIVE = {"count", "sum"}


def _pick_dimension(payload: dict[str, Any], spatial_dim: str) -> str | None:
    dims = payload.get("agg_dimensions", [])
    preferred = {
        "page": "page_url",
        "element": "item_id",
        "region": "region",
    }.get(spatial_dim)
    if preferred and preferred in dims:
        return preferred
    return dims[0] if dims else None


def _color_for(level: float) -> str:
    if level <= 0:
        return _COLOR_STOPS[0]
    index = min(int(level * (len(_COLOR_STOPS) - 1)), len(_COLOR_STOPS) - 2)
    return _COLOR_STOPS[index + 1]


def _combine_values(values: list[float], agg_function: str) -> float:
    if not values:
        return 0
    if agg_function in _ADDITIVE:
        return sum(values)
    if agg_function == "max":
        return max(values)
    if agg_function == "min":
        return min(values)
    # avg / uv 跨桶不可加，只能给出近似值，调用方会标记 approximate。
    if agg_function == "avg":
        return sum(values) / len(values)
    return max(values)


def _parse_config_range(time_range: str) -> tuple[str, str]:
    """把配置里的 ``A/B`` 时间范围拆成开始与结束。

    只有日期时按整天处理：结束日期交给解析器顺延到次日零点，保证最后一小时
    是完整小时桶，而不是被 23:59:59 截断。
    """
    raw = (time_range or "").strip()
    if "/" not in raw:
        raise ValueError(f"热力配置的时间范围无法解析：{time_range}")
    start, end = (part.strip() for part in raw.split("/", 1))
    if "T" not in start and " " not in start:
        start = f"{start}T00:00:00"
    return start, end


def generate_heatmap(
    config_id: str,
    start: str | None = None,
    end: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """生成行为热力图，基于同一聚合版本重算格值与色阶。"""
    config = store.find_by_field("behavior_heatmap", "config_id", config_id)
    if config is None:
        raise ValueError(f"Heatmap config not found: {config_id}")

    event_id = config["event_id"]
    spatial_dim = config["spatial_dim"]
    agg_grain = config["agg_grain"]

    # 选定要消费的事件聚合配置：显式指定优先，否则按事件取。
    aggregate_config = config.get("aggregate_config_id") or ""
    if not aggregate_config:
        candidates = [
            row for row in store.rows("event_aggregate")
            if row.get("event_id") == event_id
        ]
        if candidates:
            aggregate_config = candidates[0]["config_id"]

    if not aggregate_config:
        result = {
            "config_id": config_id,
            "event_id": event_id,
            "spatial_dim": spatial_dim,
            "agg_grain": agg_grain,
            "time_range": config["time_range"],
            "heatmap_data": {},
            "cell_index": [],
            "color_scale": config["color_scale"],
            "scale_min": 0,
            "scale_max": 0,
            "scale_source": "none",
            "source_aggregate": None,
            "aggregate_version": None,
            "warning": f"事件 {event_id} 没有可用的事件聚合配置",
            "output_image": config.get("output_image", ""),
        }
        store.set_artifact("behavior_heatmap", config_id, result)
        store.update("behavior_heatmap", config["id"], {"status": "已完成", "pending": False})
        return result

    # 生成开始时钉住一个版本，后续所有格子都只读这一份快照。
    payload = get_aggregate_version(aggregate_config, version)
    pinned_version = payload["version"]

    range_start, range_end = _parse_config_range(config["time_range"])
    start = start or range_start
    end = end or range_end

    detail = query_aggregate_detail(
        aggregate_config,
        start=start,
        end=end,
        version=pinned_version,
    )

    dimension = _pick_dimension(payload, spatial_dim)
    agg_function = payload.get("agg_function", "count")

    # 每个维度值 × 每个时间桶 = 一个格子，格子带上它实际覆盖的时间桶。
    cells: list[dict[str, Any]] = []
    heatmap_data: dict[str, list[dict[str, Any]]] = {}
    for bucket in detail["buckets"]:
        grouped: dict[str, list[float]] = {}
        for row in bucket["rows"]:
            key = str(row.get(dimension, "unknown")) if dimension else "all"
            grouped.setdefault(key, []).append(float(row.get("agg_value", 0)))
        for key, values in grouped.items():
            value = _combine_values(values, agg_function)
            cells.append({
                "dimension": key,
                "time_slot": bucket["bucket"],
                "value": round(value, 4),
                "intensity": round(value, 4),
                "coverage_status": bucket["coverage_status"],
                "coverage_ratio": bucket["coverage_ratio"],
                "source": bucket["source"],
                "exact": bucket["exact"],
                "comparable": bucket["comparable"],
                "provisional": bucket["provisional"],
                "approximate": agg_function not in _ADDITIVE and len(values) > 1,
                "covered_buckets": [{
                    "bucket": bucket["bucket"],
                    "covered_start": bucket["covered_start"],
                    "covered_end": bucket["covered_end"],
                    "coverage_status": bucket["coverage_status"],
                    "coverage_ratio": bucket["coverage_ratio"],
                    "source": bucket["source"],
                    "exact": bucket["exact"],
                }],
                "note": bucket["note"],
            })

    # 色阶只由可比较的格子决定；缺覆盖的格子保留但排除在色阶之外。
    comparable_cells = [cell for cell in cells if cell["comparable"]]
    if comparable_cells:
        scale_min = min(cell["value"] for cell in comparable_cells)
        scale_max = max(cell["value"] for cell in comparable_cells)
        scale_source = "comparable_cells_only"
    else:
        scale_min = scale_max = 0
        scale_source = "no_comparable_cells"
    span = scale_max - scale_min

    for cell in cells:
        if cell["comparable"] and span > 0:
            level = (cell["value"] - scale_min) / span
        elif cell["comparable"]:
            level = 1.0 if cell["value"] > 0 else 0.0
        else:
            level = 0.0
        cell["color_level"] = round(level, 4)
        cell["color"] = _color_for(level) if cell["comparable"] else "#e5e7eb"
        cell["in_color_scale"] = cell["comparable"]
        heatmap_data.setdefault(cell["dimension"], []).append(cell)

    # 只有同属可比较集合的桶才能拿来横向比较。
    comparisons: list[dict[str, Any]] = []
    comparable_buckets = [
        bucket["bucket"] for bucket in detail["buckets"] if bucket["comparable"]
    ]
    for i, bucket_a in enumerate(comparable_buckets):
        for bucket_b in comparable_buckets[i + 1:]:
            for dim_value, dim_cells in heatmap_data.items():
                by_slot = {cell["time_slot"]: cell for cell in dim_cells}
                if bucket_a in by_slot and bucket_b in by_slot:
                    comparisons.append({
                        "dimension": dim_value,
                        "bucket_a": bucket_a,
                        "bucket_b": bucket_b,
                        "value_a": by_slot[bucket_a]["value"],
                        "value_b": by_slot[bucket_b]["value"],
                        "delta": round(by_slot[bucket_b]["value"] - by_slot[bucket_a]["value"], 4),
                        "comparable": True,
                    })

    # 被排除在比较之外的格子要明说原因，运营才知道哪些不能拿来比。
    excluded_cells = [
        {
            "dimension": cell["dimension"],
            "time_slot": cell["time_slot"],
            "coverage_status": cell["coverage_status"],
            "coverage_ratio": cell["coverage_ratio"],
            "reason": cell["note"],
        }
        for cell in cells
        if not cell["comparable"]
    ]

    # 完全没有落出格子的时间桶（如只剩整桶统计、区间缺原始记录）也要显式列出，
    # 否则热力图上「少了一列」会被误读成没有行为。
    buckets_with_cells = {
        bucket["bucket"] for bucket in detail["buckets"] if bucket["rows"]
    }
    unavailable_buckets = [
        {
            "bucket": bucket["bucket"],
            "coverage_status": bucket["coverage_status"],
            "coverage_ratio": bucket["coverage_ratio"],
            "source": bucket["source"],
            "reason": bucket["note"],
        }
        for bucket in detail["buckets"]
        if bucket["bucket"] not in buckets_with_cells
    ]

    result = {
        "config_id": config_id,
        "event_id": event_id,
        "spatial_dim": spatial_dim,
        "dimension": dimension,
        "agg_grain": agg_grain,
        "time_range": f"{start}/{end}",
        "time_window": {
            "start": detail["start"],
            "end": detail["end"],
            "covered_buckets": [bucket["bucket"] for bucket in detail["buckets"]],
        },
        "heatmap_data": heatmap_data,
        "cell_index": cells,
        "max_intensity": scale_max,
        "min_intensity": scale_min,
        "color_scale": config["color_scale"],
        "scale_min": scale_min,
        "scale_max": scale_max,
        "scale_source": scale_source,
        "scale_excluded_cells": [
            {"dimension": item["dimension"], "time_slot": item["time_slot"],
             "reason": item["coverage_status"]}
            for item in excluded_cells
        ],
        "comparisons": comparisons,
        "excluded_cells": excluded_cells,
        "unavailable_buckets": unavailable_buckets,
        "comparable_buckets": detail["comparable_buckets"],
        "incomparable_buckets": detail["incomparable_buckets"],
        "coverage": detail["coverage"],
        "source_aggregate": aggregate_config,
        "aggregate_version": pinned_version,
        "source_versions": [pinned_version],
        "version_locked": True,
        "output_image": config.get("output_image", ""),
    }

    store.set_artifact("behavior_heatmap", config_id, result)
    store.update("behavior_heatmap", config["id"], {"status": "已完成", "pending": False})
    return result


def get_heatmap(config_id: str) -> dict[str, Any] | None:
    """获取热力图结果。"""
    artifact = store.get_artifact("behavior_heatmap", config_id)
    if artifact:
        return artifact
    try:
        return generate_heatmap(config_id)
    except ValueError:
        return None
