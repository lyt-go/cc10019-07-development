"""事件聚合引擎：消费清洗后的事件数据，执行聚合计算。

聚合产物以「版本快照」的形式落库：每次执行都会生成一个不可变的版本，
下游（如行为热力）只能读取某一个版本，不能在查询中途混用新旧数据。

时间桶支持两种粒度的原始数据：

* 整桶统计：小时桶的总量，只有在该小时的数据已经补齐时才能代表整个小时；
* 分钟明细：桶内每分钟的原始记录，仅在配置 ``detail_retention_hours``
  列出的小时保留。

区间查询会按「查询区间与小时桶的交集」逐桶判断覆盖情况。区间只覆盖桶的
一部分且该小时没有分钟明细时，返回不可用，而不是把整小时总数摊到区间上。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app.schemas import AggResult
from app.store import store

DATA_DATE = date(2026, 11, 15)
PAGES = ["/home", "/product", "/cart", "/checkout", "/profile"]
DEVICES = ["ios", "android", "pc", "other"]
MEMBERS = ["普通", "银卡", "金卡", "钻石"]

# 十点这一小时仍在补录迟到事件：15 分及以后整段缺失。
LATE_HOUR = 10
LATE_GAP_FROM_MINUTE = 15


# ========== 原始数据生成 ==========

def _seed_for(event_id: str) -> int:
    """由事件编号派生的稳定种子，保证同一天的数据可复现。"""
    return sum((i + 1) * ord(ch) for i, ch in enumerate(event_id))


def _source_events(event_id: str) -> list[dict[str, Any]]:
    """生成某一天的完整源事件（包含迟到记录）。"""
    seed = _seed_for(event_id)
    events: list[dict[str, Any]] = []
    for hour in range(24):
        for page_idx, page in enumerate(PAGES):
            count = 6 + ((hour * 7 + page_idx * 5 + seed) % 5)
            for i in range(count):
                minute = (page_idx * 13 + i * 7 + hour * 11 + seed) % 60
                is_late = hour == LATE_HOUR and minute >= LATE_GAP_FROM_MINUTE
                events.append({
                    "user_id": f"U{(hour * 100 + page_idx * 20 + i) % 900 + 100:05d}",
                    "event_id": event_id,
                    "timestamp": datetime(
                        DATA_DATE.year, DATA_DATE.month, DATA_DATE.day, hour, minute
                    ),
                    "page_url": page,
                    "ATTR-001": ["app", "web", "miniapp", "h5"][i % 4],
                    "ATTR-004": DEVICES[(i + page_idx + hour) % 4],
                    "ATTR-005": MEMBERS[(i + page_idx) % 4],
                    "value": float(((hour + 1) * 10 + i) * 1.5),
                    "late": is_late,
                })
    return events


def _pending_backfill_hours(config_id: str) -> set[int]:
    """仍未完成补录的小时。"""
    config = store.find_by_field("event_aggregate", "config_id", config_id) or {}
    pending = set(config.get("pending_backfill_hours", []))
    state = store.get_artifact("event_aggregate_ingest", config_id) or {}
    return pending - set(state.get("backfilled_hours", []))


def _available_events(config_id: str, event_id: str) -> list[dict[str, Any]]:
    """当前已入库的事件：未补录完成的迟到记录还不可见。"""
    pending = _pending_backfill_hours(config_id)
    return [
        evt for evt in _source_events(event_id)
        if not (evt["late"] and evt["timestamp"].hour in pending)
    ]


# ========== 聚合计算 ==========

def _apply_agg(events: list[dict[str, Any]], agg_function: str) -> float | int:
    if agg_function == "count":
        return len(events)
    if agg_function == "sum":
        return sum(e.get("value", 0) for e in events)
    if agg_function == "avg":
        vals = [e.get("value", 0) for e in events]
        return sum(vals) / len(vals) if vals else 0
    if agg_function == "max":
        return max((e.get("value", 0) for e in events), default=0)
    if agg_function == "min":
        return min((e.get("value", 0) for e in events), default=0)
    if agg_function == "uv":
        return len({e.get("user_id") for e in events})
    return len(events)


def _group_rows(
    events: Iterable[dict[str, Any]],
    agg_dimensions: list[str],
    agg_function: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for evt in events:
        key = tuple(str(evt.get(dim, "unknown")) for dim in agg_dimensions)
        grouped[key].append(evt)
    rows: list[dict[str, Any]] = []
    for key in sorted(grouped):
        row: dict[str, Any] = {dim: key[i] for i, dim in enumerate(agg_dimensions)}
        row["agg_value"] = _apply_agg(grouped[key], agg_function)
        rows.append(row)
    return rows


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _slot(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:00")


def _build_version_payload(config: dict[str, Any]) -> dict[str, Any]:
    config_id = config["config_id"]
    event_id = config["event_id"]
    agg_dimensions = config.get("agg_dimensions", [])
    agg_function = config["agg_function"]
    detail_hours = sorted(set(config.get("detail_retention_hours", [])))
    pending = _pending_backfill_hours(config_id)
    events = _available_events(config_id, event_id)

    version_number = 1 + sum(
        1 for key in store.get_artifact_keys("event_aggregate_version")
        if key.startswith(f"{config_id}@v")
    )
    version = f"{config_id}@v{version_number}"

    # 整桶统计：每个小时算出该小时的全部维度组合。
    bucket_stats: dict[str, dict[str, Any]] = {}
    legacy_rows: list[dict[str, Any]] = []
    by_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for evt in events:
        by_hour[evt["timestamp"].hour].append(evt)

    for hour in sorted(by_hour):
        bucket = datetime(DATA_DATE.year, DATA_DATE.month, DATA_DATE.day, hour)
        # 未补齐的小时只落已入库的部分，绝不用「已到记录 × 外推」伪装成整桶总数。
        rows = _group_rows(by_hour[hour], agg_dimensions, agg_function)
        complete = hour not in pending
        bucket_stats[_slot(bucket)] = {
            "hour": hour,
            "complete": complete,
            "rows": rows,
            "event_count": len(by_hour[hour]),
            "covered_until": (
                _iso(bucket + timedelta(hours=1))
                if complete
                else _iso(bucket + timedelta(minutes=LATE_GAP_FROM_MINUTE))
            ),
        }
        for row in rows:
            legacy_row = dict(row)
            legacy_row["time_grain"] = bucket.strftime("%H:00")
            legacy_row["bucket_complete"] = complete
            legacy_rows.append(legacy_row)

    # 分钟明细：只为保留明细的小时落库。
    detail_records: list[dict[str, Any]] = []
    for hour in detail_hours:
        for evt in by_hour.get(hour, []):
            detail_records.append({
                "timestamp": _iso(evt["timestamp"]),
                "hour": hour,
                "minute": evt["timestamp"].replace(second=0, microsecond=0),
                "dimensions": {dim: evt.get(dim, "unknown") for dim in agg_dimensions},
                "user_id": evt.get("user_id"),
                "value": evt.get("value", 0),
            })

    payload = {
        "config_id": config_id,
        "event_id": event_id,
        "agg_dimensions": agg_dimensions,
        "agg_function": agg_function,
        "time_grain": config["time_grain"],
        "version": version,
        "version_number": version_number,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "data_date": DATA_DATE.isoformat(),
        "detail_hours": detail_hours,
        "pending_backfill_hours": sorted(pending),
        "bucket_stats": bucket_stats,
        "detail_records": detail_records,
        "result_rows": legacy_rows,
        "total_count": len(legacy_rows),
        "output_table": config.get("output_table", ""),
    }
    return payload


def aggregate_events(config_id: str) -> AggResult:
    """执行事件聚合，并发布一个不可变版本。"""
    config = store.find_by_field("event_aggregate", "config_id", config_id)
    if config is None:
        raise ValueError(f"Aggregate config not found: {config_id}")

    payload = _build_version_payload(config)

    # 版本账本 + 最新指针；下游通过版本号读取，避免读到半新半旧的数据。
    store.set_artifact("event_aggregate_version", payload["version"], payload)
    store.set_artifact("event_aggregate", config_id, payload)

    store.update("event_aggregate", config["id"], {
        "status": "已生效",
        "pending": False,
    })

    return AggResult(
        config_id=config_id,
        event_id=payload["event_id"],
        agg_dimensions=payload["agg_dimensions"],
        time_grain=payload["time_grain"],
        result_rows=payload["result_rows"],
        total_count=payload["total_count"],
        output_table=payload["output_table"],
        version=payload["version"],
        detail_hours=payload["detail_hours"],
        whole_hours=[
            int(slot[11:13]) for slot, stat in payload["bucket_stats"].items()
            if stat["complete"] and int(slot[11:13]) not in payload["detail_hours"]
        ],
    )


def get_aggregate_result(config_id: str) -> dict[str, Any] | None:
    """获取最新版本的聚合结果。"""
    artifact = store.get_artifact("event_aggregate", config_id)
    if artifact:
        return artifact
    try:
        return aggregate_events(config_id).model_dump()
    except ValueError:
        return None


def get_aggregate_version(config_id: str, version: str | None = None) -> dict[str, Any]:
    """按版本号取快照；不传版本号时取当前最新版本。"""
    if version:
        payload = store.get_artifact("event_aggregate_version", version)
        if payload is None:
            raise ValueError(f"Aggregate version not found: {version}")
        if payload.get("config_id") != config_id:
            raise ValueError(f"Version {version} does not belong to {config_id}")
        return payload
    latest = store.get_artifact("event_aggregate", config_id)
    if latest is not None:
        return latest
    aggregate_events(config_id)
    return store.get_artifact("event_aggregate", config_id) or {}


def backfill_hour(config_id: str, hour: int) -> AggResult:
    """补齐某个小时的迟到事件，并发布新版本。"""
    config = store.find_by_field("event_aggregate", "config_id", config_id)
    if config is None:
        raise ValueError(f"Aggregate config not found: {config_id}")
    if not 0 <= hour <= 23:
        raise ValueError(f"Invalid hour: {hour}")

    state = store.get_artifact("event_aggregate_ingest", config_id) or {}
    backfilled = set(state.get("backfilled_hours", []))
    backfilled.add(hour)
    store.set_artifact("event_aggregate_ingest", config_id, {
        "backfilled_hours": sorted(backfilled),
    })
    return aggregate_events(config_id)


# ========== 区间查询 ==========

def _parse_bound(value: str, data_date: date, is_end: bool = False) -> datetime:
    """解析查询边界：支持完整时间、仅时刻，以及只有日期。"""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("start/end 不能为空")
    normalized = raw.replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return datetime.combine(data_date, parsed.time())
    try:
        parsed_date = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"无法解析时间边界：{value}") from exc
    if is_end:
        # 只有日期时按「次日零点」作为开区间右端，避免把最后一个小时截成 59:59。
        return datetime.combine(parsed_date + timedelta(days=1), datetime.min.time())
    return datetime.combine(parsed_date, datetime.min.time())


def _bucket_coverage(
    payload: dict[str, Any],
    bucket_start: datetime,
    bucket_end: datetime,
    range_start: datetime,
    range_end: datetime,
) -> dict[str, Any]:
    """判断某个小时桶在查询区间内的覆盖情况。"""
    slot = _slot(bucket_start)
    stat = payload["bucket_stats"].get(slot)
    covered_start = max(bucket_start, range_start)
    covered_end = min(bucket_end, range_end)
    covered_seconds = max(0.0, (covered_end - covered_start).total_seconds())
    bucket_seconds = (bucket_end - bucket_start).total_seconds()
    full_bucket = covered_seconds >= bucket_seconds

    if stat is None:
        return {
            "status": "missing",
            "source": "unavailable",
            "coverage_ratio": 0.0,
            "exact": False,
            "comparable": False,
            "note": "该小时不在聚合数据的日期范围内",
        }

    hour = stat["hour"]
    has_detail = hour in payload["detail_hours"]

    if full_bucket:
        if stat["complete"]:
            return {
                "status": "complete",
                "source": "whole_bucket",
                "coverage_ratio": 1.0,
                "exact": True,
                "comparable": True,
                "note": "整点查询命中完整整桶统计",
            }
        return {
            "status": "partial",
            "source": "whole_bucket",
            # 整桶仍缺记录，水位只到已入库的分钟；覆盖比例按水位相对整个桶算。
            "coverage_ratio": round(LATE_GAP_FROM_MINUTE / 60, 4),
            "exact": False,
            "comparable": False,
            "note": "该小时仍在补录，整桶统计尚不完整",
        }

    if not has_detail:
        return {
            "status": "missing_detail",
            "source": "unavailable",
            "coverage_ratio": 0.0,
            "exact": False,
            "comparable": False,
            "note": "该小时只保留整桶统计，缺少区间内的原始记录；不能用整小时总数代替区间值",
        }

    # 分钟明细只覆盖到入库水位；水位之后的记录尚未补录。按水位而非已到
    # 记录的时间集合算覆盖，避免「零散迟到」把缺口掩盖成完整。
    ingested_from = bucket_start
    ingested_to = bucket_start + timedelta(
        hours=1,
    ) if stat["complete"] else bucket_start + timedelta(minutes=LATE_GAP_FROM_MINUTE)
    ingested_seconds = max(
        0.0,
        (min(ingested_to, covered_end) - max(ingested_from, covered_start)).total_seconds(),
    )
    ratio = (ingested_seconds / covered_seconds) if covered_seconds else 0.0
    complete = ratio >= 1.0
    return {
        "status": "complete" if complete else "partial",
        "source": "minute_detail",
        "coverage_ratio": round(min(ratio, 1.0), 4),
        "exact": complete,
        "comparable": complete,
        "note": (
            "区间内分钟明细完整"
            if complete
            else "区间内分钟明细不完整（仍有迟到事件待补录），结果仅为下界"
        ),
    }


def _rows_from_detail(
    payload: dict[str, Any],
    hour: int,
    range_start: datetime,
    range_end: datetime,
) -> list[dict[str, Any]]:
    """用分钟明细重算区间内每个维度组合的聚合值。"""
    dims = payload["agg_dimensions"]
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for rec in payload["detail_records"]:
        if rec["hour"] != hour:
            continue
        if not (range_start <= rec["minute"] < range_end):
            continue
        key = tuple(str(rec["dimensions"].get(dim, "unknown")) for dim in dims)
        grouped[key].append(rec)
    rows: list[dict[str, Any]] = []
    for key in sorted(grouped):
        row: dict[str, Any] = {dim: key[i] for i, dim in enumerate(dims)}
        row["agg_value"] = _apply_agg(grouped[key], payload["agg_function"])
        rows.append(row)
    return rows


def query_aggregate_detail(
    config_id: str,
    start: str,
    end: str,
    version: str | None = None,
    dimension: str | None = None,
    dimension_value: str | None = None,
) -> dict[str, Any]:
    """按开始/结束时间查询事件聚合明细，逐小时桶给出覆盖情况。

    返回的每个桶都带有 ``exact`` / ``comparable`` 标记：只有覆盖完整的小时
    桶才参与区间比较；缺少原始记录的部分桶不会用整桶统计凑数。
    """
    payload = get_aggregate_version(config_id, version)
    data_date = date.fromisoformat(payload["data_date"])
    range_start = _parse_bound(start, data_date)
    range_end = _parse_bound(end, data_date, is_end=True)
    if range_end <= range_start:
        raise ValueError("end 必须晚于 start")

    dims = payload["agg_dimensions"]

    def keep(row: dict[str, Any]) -> bool:
        if dimension and dimension_value is not None:
            return str(row.get(dimension)) == str(dimension_value)
        return True

    buckets: list[dict[str, Any]] = []
    cursor = range_start.replace(minute=0, second=0, microsecond=0)
    while cursor < range_end:
        bucket_start = cursor
        bucket_end = cursor + timedelta(hours=1)
        covered_start = max(bucket_start, range_start)
        covered_end = min(bucket_end, range_end)
        coverage = _bucket_coverage(
            payload, bucket_start, bucket_end, range_start, range_end
        )
        hour = bucket_start.hour

        rows: list[dict[str, Any]] = []
        if coverage["source"] == "whole_bucket":
            stat = payload["bucket_stats"].get(_slot(bucket_start)) or {}
            for row in stat.get("rows", []):
                if keep(row):
                    rows.append(dict(row))
        elif coverage["source"] == "minute_detail":
            rows = [row for row in _rows_from_detail(
                payload, hour, covered_start, covered_end
            ) if keep(row)]

        buckets.append({
            "bucket": _slot(bucket_start),
            "bucket_start": _iso(bucket_start),
            "bucket_end": _iso(bucket_end),
            "covered_start": _iso(covered_start),
            "covered_end": _iso(covered_end),
            "coverage_status": coverage["status"],
            "coverage_ratio": coverage["coverage_ratio"],
            "source": coverage["source"],
            "exact": coverage["exact"],
            "comparable": coverage["comparable"],
            "provisional": not coverage["exact"],
            "note": coverage["note"],
            "rows": rows,
        })
        cursor = bucket_end

    coverage_summary = [
        {
            "bucket": bucket["bucket"],
            "coverage_status": bucket["coverage_status"],
            "coverage_ratio": bucket["coverage_ratio"],
            "source": bucket["source"],
            "exact": bucket["exact"],
            "comparable": bucket["comparable"],
        }
        for bucket in buckets
    ]
    return {
        "config_id": config_id,
        "event_id": payload["event_id"],
        "version": payload["version"],
        "data_date": payload["data_date"],
        "agg_dimensions": dims,
        "agg_function": payload["agg_function"],
        "time_grain": "minute",
        "start": _iso(range_start),
        "end": _iso(range_end),
        "detail_hours": payload["detail_hours"],
        "pending_backfill_hours": payload.get("pending_backfill_hours", []),
        "buckets": buckets,
        "coverage": coverage_summary,
        "comparable_buckets": [
            item["bucket"] for item in coverage_summary if item["comparable"]
        ],
        "incomparable_buckets": [
            item["bucket"] for item in coverage_summary if not item["comparable"]
        ],
        "row_count": sum(len(bucket["rows"]) for bucket in buckets),
    }
