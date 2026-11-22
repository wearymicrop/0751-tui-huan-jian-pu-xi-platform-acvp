"""把召回影响换算成逐件处置任务，并生成可复核的下发依据。"""
import hashlib
from collections import Counter

from app.analysis import analyze, with_recall
from app.config import RULE_VERSION
from app.errors import BusinessError
from app.identity import token
from app.manifest import canonical
from app.timebox import moment

PLANNER_VERSION = "1.0"

# 现场状态 -> 处置动作。争议件先复核归属，在装件必须到现场拆出。
ACTIONS = {
    "disputed": ("hold_review", "争议件复核", "review", "待判", "P0"),
    "mounted": ("teardown_quarantine", "现场拆出并隔离", "site", "现场", "P0"),
    "stock": ("inspect_quarantine", "库内核查并隔离", "warehouse", "库房", "P1"),
    "returned": ("inspect_quarantine", "退回件核查并隔离", "warehouse", "库房", "P1"),
}
DEFAULT_ACTION = ("inspect_quarantine", "核查并隔离", "warehouse", "库房", "P1")
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def source_digest(source):
    return hashlib.sha256(canonical(source).encode("utf-8")).hexdigest()


def normalize_query(batch, start=None, end=None):
    """规范化查询条件。缺省起止保持为空，由每次重算按当下数据取默认窗。"""
    try:
        batch = token(batch)
        left = moment(start) if start is not None else None
        right = moment(end) if end is not None else None
    except (TypeError, ValueError, OverflowError) as error:
        raise BusinessError("BAD_QUERY", "批号或查询时间无效") from error
    if left is not None and right is not None and left >= right:
        raise BusinessError("BAD_WINDOW", "查询起点须早于终点")
    return {"batch": batch, "start": left, "end": right}


def dispatch_id_for(query):
    """同一召回条件（批号 + 时间窗）只对应一条下发批次。"""
    return "D-" + hashlib.sha256(canonical({
        "kind": "recall-dispatch", "query": query,
    }).encode("utf-8")).hexdigest()[:16]


def content_digest(task):
    """任务的处置身份：只包含现场要做什么、对哪件做、依据哪一处归属。

    暴露路径只作随附证据，路径补全不该让同一件同一动作的现场任务换号。
    """
    return hashlib.sha256(canonical({
        key: value for key, value in task.items()
        if key not in ("task_id", "basis", "exposures")
    }).encode("utf-8")).hexdigest()


def build_plan(source, query):
    """算出某一份源数据上的完整任务清单。

    任务号只由任务内容决定，因此同一数据重算会得到同一批任务号；
    内容发生变化的任务会换新号，旧号在上一版里原样保留以便追溯。
    """
    digest = source_digest(source)
    result = with_recall(analyze(source), query["batch"], query["start"], query["end"])
    found = result["recall"]
    states = {row["serial"]: row for row in result["balance"]["rows"]}
    exposures = {}
    for evidence in found["paths"]:
        exposures.setdefault(evidence["serial"], []).append({
            "origin": evidence["origin"],
            "path": list(evidence["path"]),
            "start": evidence["start"],
            "end": evidence["end"],
            "mount_rows": list(evidence["mount_rows"]),
        })
    contents = []
    for serial in found["targets"]:
        position = states.get(serial, {})
        state = position.get("state", "stock")
        code, label, lane, lane_label, priority = ACTIONS.get(state, DEFAULT_ACTION)
        contents.append({
            "serial": serial,
            "batch": position.get("batch"),
            "kind": position.get("kind"),
            "registered": position.get("registered"),
            "action": code,
            "action_label": label,
            "lane": lane,
            "lane_label": lane_label,
            "priority": priority,
            "state": state,
            "active_parent": position.get("active_parent"),
            "reason_codes": list(position.get("reason_codes") or []),
            "exposures": sorted(exposures.get(serial, []),
                                key=lambda row: (row["start"], row["path"], row["mount_rows"])),
        })
    for content in contents:
        content["task_id"] = "T-" + content_digest(content)[:14]
    contents.sort(key=lambda task: (PRIORITY_ORDER.get(task["priority"], 9), task["serial"]))
    warnings = []
    if found["excluded_origins"]:
        warnings.append({
            "code": "DISPUTED_ORIGIN",
            "message": "起点档案批号有争议，未计入影响，需人工确认",
            "serials": list(found["excluded_origins"]),
        })
    disputed = [task["serial"] for task in contents if task["state"] == "disputed"]
    if disputed:
        warnings.append({
            "code": "DISPUTED_TARGET",
            "message": "目标件本身处于争议状态，现场处置前需先裁决归属",
            "serials": disputed,
        })
    basis = {
        "kind": "recall-dispatch",
        "query": dict(query),
        "window": {"start": found["start"], "end": found["end"]},
        "rule_version": RULE_VERSION,
        "planner_version": PLANNER_VERSION,
        "source_digest": digest,
        "source_counts": {field: len(source.get(field) or [])
                          for field in ("parts", "aliases", "returns", "events")},
        "recall": {"count": found["count"], "targets": list(found["targets"]),
                   "excluded_origins": list(found["excluded_origins"])},
        "warnings": warnings,
    }
    revision_id = "R-" + hashlib.sha256(canonical({
        "basis": basis,
        "tasks": sorted(({key: value for key, value in task.items() if key != "task_id"}
                         for task in contents), key=lambda task: task["serial"]),
    }).encode("utf-8")).hexdigest()[:20]
    summary = {
        "tasks": len(contents),
        "by_action": dict(Counter(task["action"] for task in contents)),
        "by_lane": dict(Counter(task["lane"] for task in contents)),
        "by_priority": dict(Counter(task["priority"] for task in contents)),
    }
    return {"revision_id": revision_id, "basis": basis, "tasks": contents, "summary": summary}


def build_delta(previous, current):
    """按任务号比较相邻两版：保留、撤回、新增。"""
    old = {task["task_id"]: task for task in previous or []}
    new = {task["task_id"]: task for task in current or []}
    retained = [new[key] for key in sorted(new.keys() & old.keys())]
    withdrawn = [old[key] for key in sorted(old.keys() - new.keys())]
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    return {
        "retained": retained,
        "withdrawn": withdrawn,
        "added": added,
        "counts": {"retained": len(retained), "withdrawn": len(withdrawn), "added": len(added)},
    }
