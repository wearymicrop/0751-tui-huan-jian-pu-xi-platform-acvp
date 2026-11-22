"""合并晚到的补录数据。

补录只追加，不覆盖首条记录；完全相同的重复上报会被忽略。
原行在前，补录行按原顺序追加在后，行号保持不变。
"""
from app.errors import BusinessError
from app.manifest import canonical
from app.source import validate_source
from app.timebox import moment

FIELDS = ("parts", "aliases", "returns", "events")


def validate_supplement(payload):
    """补录可以只带部分数组，但给出的数组必须是合法列表。"""
    if not isinstance(payload, dict):
        raise BusinessError("BAD_SOURCE", "补录数据须为 JSON 对象")
    if not any(field in payload for field in FIELDS):
        raise BusinessError("BAD_SOURCE", "补录数据至少要包含一个数组")
    for field in FIELDS:
        if field not in payload:
            continue
        rows = payload[field]
        if not isinstance(rows, list) or len(rows) > 2500:
            raise BusinessError("BAD_SOURCE", f"{field} 须为最多 2500 条的数组")
    if payload.get("as_of") is not None:
        try:
            moment(payload["as_of"])
        except (TypeError, ValueError, OverflowError) as error:
            raise BusinessError("BAD_TIME", "请填写带时区的盘点时间") from error


def merge_source(original, supplement):
    """返回合并后的源数据与各数组新增条数。"""
    validate_source(original)
    validate_supplement(supplement)
    merged = dict(original)
    stats = {"added": {}, "duplicate": 0, "before": {}, "after": {}}
    for field in FIELDS:
        rows = list(original.get(field) or [])
        seen = {canonical(row) for row in rows}
        added = 0
        for row in supplement.get(field) or []:
            key = canonical(row)
            if key in seen:
                stats["duplicate"] += 1
                continue
            seen.add(key)
            rows.append(row)
            added += 1
        merged[field] = rows
        stats["added"][field] = added
        stats["before"][field] = len(original.get(field) or [])
        stats["after"][field] = len(rows)
    if supplement.get("default_batch") is not None:
        merged.setdefault("default_batch", supplement["default_batch"])
    # 盘点时刻只向前推进：补录若带来更晚的快照，就采用它。
    points = [value for value in (original.get("as_of"), supplement.get("as_of")) if value]
    if points:
        merged["as_of"] = max(points, key=moment)
    return merged, stats
