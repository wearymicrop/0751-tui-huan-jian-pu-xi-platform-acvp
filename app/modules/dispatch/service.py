"""处置任务下发：生成、补录重算、断点续跑与追溯。

- 每次补录都基于「初始源数据 + 全部已收补录」重算完整影响，不冻结首版范围。
- 任务号只由处置内容（件、动作、状态、有效父件）决定。内容不变的任务原样保留；
  内容变化的任务撤回旧号、另发新号，现场不会同时收到互相冲突的同号任务。
- 合并、重算、落库在同一个 ``BEGIN IMMEDIATE`` 写事务内完成，并发补录不会互相
  覆盖，也不会把同一件事派两遍。
- 所有结果以修订追加保存，从不覆盖旧版；任一版任务都能按修订号追溯。
- 同一召回（批号 + 时间窗）只有一条下发批次；重跑、重复补录、断点续跑都靠
  批次号 / 修订号 / 作业号 / 任务号四层去重。
"""
import hashlib
from datetime import datetime, timezone

from app.errors import BusinessError
from app.manifest import canonical
from app.modules.dispatch import store
from app.modules.dispatch.merge import merge_source, validate_supplement
from app.modules.dispatch.planner import (
    build_delta, build_plan, dispatch_id_for, normalize_query, source_digest,
)
from app.source import load_sample, validate_source

FIELDS = ("parts", "aliases", "returns", "events")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _job_id(kind, input_digest):
    """同一批次里，一种输入只对应一条作业：重复提交自然会命中同号。"""
    return "J-" + hashlib.sha256(canonical({
        "kind": kind, "input_digest": input_digest,
    }).encode("utf-8")).hexdigest()[:16]


# ---- 对外动作 -------------------------------------------------------------

def create(payload):
    """首次下发。同一召回重跑只返回既有结果，不重复派单。

    该召回已存在、但这次带来的源数据里有晚到记录时，按补录合并重算；
    新结果仍以修订追加，不会覆盖现场已收到的任务。
    """
    source, query = _read_create(payload)
    digest = source_digest(source)
    dispatch_id = dispatch_id_for(query)
    stamp = _now()
    with store.connection(immediate=True) as db:
        record = store.load_dispatch(db, dispatch_id)
        if record is None:
            store.insert_dispatch(db, dispatch_id, query, source, stamp)
            store.record_input(db, dispatch_id, "initial", digest, _counts(source), stamp)
            fresh = True
            same = True
        else:
            fresh = False
            same = source_digest(record["source"]) == digest
    if fresh or same:
        return _perform(dispatch_id, "initial", digest)
    # 同一召回再次提交，且带来了晚到记录：按补录合并重算。
    return _perform(dispatch_id, "resubmit", digest, payload=source)


def refresh(dispatch_id):
    """按已存源数据重算，并续跑上次未落地的作业。"""
    _require(dispatch_id)
    with store.connection() as db:
        digest = source_digest(store.load_dispatch(db, dispatch_id)["source"])
    return _perform(dispatch_id, "refresh", digest)


def supplement(dispatch_id, payload):
    """补入晚到的拆装/复入库记录，立即重算受影响任务；相同补录只记一次账。"""
    _require(dispatch_id)
    validate_supplement(payload)
    return _perform(dispatch_id, "supplement", source_digest(payload), payload=payload)


def retry_job(dispatch_id, job_id):
    """续跑一个未落地的作业；已落地则原样返回，不重复派单。"""
    _require(dispatch_id)
    with store.connection() as db:
        job = store.load_job(db, dispatch_id, job_id)
    if job is None:
        raise BusinessError("UNKNOWN_JOB", "找不到该下发作业", 404)
    if job["status"] == "applied":
        return _snapshot(dispatch_id, {"status": "reused", "reused": True, "job_id": job_id})
    if job["status"] == "superseded":
        raise BusinessError("STALE_JOB", "该作业已被更新的修订取代", 409)
    if job["payload"] is None:
        raise BusinessError("STALE_JOB", "该作业没有可续跑的任务快照", 409)
    outcome = _run_job(dispatch_id, job)
    outcome["job_id"] = job_id
    return _snapshot(dispatch_id, outcome)


def detail(dispatch_id):
    _require(dispatch_id)
    return _snapshot(dispatch_id, {"status": "ok"})


def revision(dispatch_id, seq):
    _require(dispatch_id)
    with store.connection() as db:
        record = store.get_revision(db, dispatch_id, seq)
        if record is None:
            raise BusinessError("UNKNOWN_REVISION", "找不到该任务的修订版本", 404)
        return _revision_view(db, dispatch_id, record)


def preview(source, batch, start=None, end=None):
    """只计算不下发：页面确认范围后再派单。"""
    validate_source(source)
    query = normalize_query(batch, start, end)
    plan = build_plan(source, query)
    dispatch_id = dispatch_id_for(query)
    with store.connection() as db:
        record = store.load_dispatch(db, dispatch_id)
        latest = store.latest_revision(db, dispatch_id) if record else None
    existing = None
    if latest is not None:
        existing = {"dispatch_id": dispatch_id, "revision_seq": latest["seq"],
                    "summary": latest["plan"].get("summary", {})}
    return {
        "dispatch_id": dispatch_id,
        "batch": query["batch"],
        "query": query,
        "window": plan["basis"]["window"],
        "basis": plan["basis"],
        "revision_id": plan["revision_id"],
        "summary": plan["summary"],
        "warnings": plan["basis"]["warnings"],
        "tasks": plan["tasks"],
        "existing": existing,
        "persisted": False,
    }


def runs():
    with store.connection() as db:
        rows = store.list_dispatches(db)
    return {"dispatches": [{key: row[key] for key in
                            ("id", "batch", "start", "end", "seq", "created", "updated")}
                           for row in rows]}


# ---- 一次下发（合并 + 重算 + 落库） ---------------------------------------

def _perform(dispatch_id, kind, input_digest, payload=None):
    """先续跑遗留作业，再登记本次作业并执行。

    ``payload`` 为本次要合并的源数据；为 None 表示只按已存源数据重算。
    """
    resumed = _resume(dispatch_id)
    job = _open_job(dispatch_id, kind, input_digest, payload)
    if job is None:
        return _snapshot(dispatch_id, {"status": "reused", "reused": True,
                                       "duplicate_supplement": True, "resumed": resumed})
    if job["status"] == "applied":
        return _snapshot(dispatch_id, {"status": "reused", "reused": True,
                                       "job_id": job["job_id"], "resumed": resumed})
    outcome = _run_job(dispatch_id, job)
    outcome["resumed"] = resumed
    return _snapshot(dispatch_id, outcome)


def _open_job(dispatch_id, kind, input_digest, payload):
    """把本次输入固化成一条可续跑作业。重复补录不建新作业。"""
    stamp = _now()
    with store.connection(immediate=True) as db:
        record = store.load_dispatch(db, dispatch_id)
        if record is None:
            raise BusinessError("UNKNOWN_DISPATCH", "找不到该下发批次", 404)
        if payload is not None and store.has_supplement(db, dispatch_id, input_digest):
            store.record_input(db, dispatch_id, "duplicate_supplement", input_digest, {}, stamp)
            return None
        job = {"dispatch_id": dispatch_id, "job_id": _job_id(kind, input_digest),
               "kind": kind, "input_digest": input_digest, "payload": payload,
               "created": stamp}
        return store.save_job(db, job)


def _run_job(dispatch_id, job):
    """在单个写事务里合并输入、重算并追加修订。失败整体回滚，旧版继续有效。

    补录彼此独立：每次只合并自己的 payload，绝不删除别的未执行补录。
    """
    try:
        with store.connection(immediate=True) as db:
            current = store.load_job(db, dispatch_id, job["job_id"])
            if current is not None and current["status"] == "applied":
                record = store.load_dispatch(db, dispatch_id)
                return {"status": "reused", "reused": True,
                        "revision_seq": current["revision_seq"] or record["seq"]}
            record = store.load_dispatch(db, dispatch_id)
            if record is None:
                raise BusinessError("UNKNOWN_DISPATCH", "找不到该下发批次", 404)
            stamp = _now()
            source = record["source"]
            if job["payload"] is not None:
                source, stats = merge_source(source, job["payload"])
                store.update_source(db, dispatch_id, source, stamp)
                store.record_input(db, dispatch_id, job["kind"], job["input_digest"], stats, stamp)
            else:
                stats = None
            merges = job["kind"] in ("supplement", "resubmit")
            plan = build_plan(source, _query_of(record))
            latest = store.latest_revision(db, dispatch_id)
            if latest is not None and latest["revision_id"] == plan["revision_id"]:
                store.mark_job(db, dispatch_id, job["job_id"], "applied", None, stamp,
                               revision_seq=latest["seq"])
                if merges:
                    store.record_supplement(db, dispatch_id, job["input_digest"], job["kind"],
                                            latest["seq"], stats or {}, stamp)
                return {"status": "reused", "reused": True, "revision_seq": latest["seq"],
                        "supplement": stats}
            target_seq = (latest["seq"] if latest else 0) + 1
            basis = {**plan["basis"], "dispatch_id": dispatch_id, "revision_seq": target_seq,
                     "revision_id": plan["revision_id"],
                     "previous_seq": latest["seq"] if latest else None,
                     "input": {"kind": job["kind"], "digest": job["input_digest"]}}
            tasks = [{**task, "basis": _task_basis(basis, task)} for task in plan["tasks"]]
            store.insert_revision(db, dispatch_id, target_seq, plan["revision_id"],
                                  basis, plan, stamp)
            store.insert_tasks(db, dispatch_id, target_seq, tasks)
            store.update_seq(db, dispatch_id, target_seq, stamp)
            store.mark_job(db, dispatch_id, job["job_id"], "applied", None, stamp,
                           revision_seq=target_seq)
            if merges:
                store.record_supplement(db, dispatch_id, job["input_digest"], job["kind"],
                                        target_seq, stats or {}, stamp)
            return {"status": "applied", "revision_seq": target_seq, "supplement": stats}
    except BusinessError as error:
        _fail(dispatch_id, job["job_id"], error.code)
        raise
    except Exception as error:  # 落库失败：旧修订保持有效，作业留待续跑
        _fail(dispatch_id, job["job_id"], type(error).__name__)
        raise BusinessError("DISPATCH_FAILED", "下发未落库，旧任务仍有效，可续跑", 503) from error


def _resume(dispatch_id):
    """续跑遗留作业，保证同一召回只有一串有效任务。

    个别作业仍失败时只登记结果、不阻断本次新输入；失败的补录留在作业里，
    下次进入或显式重试时继续合并，不会被丢弃。
    """
    outcomes = []
    with store.connection() as db:
        pending = store.pending_jobs(db, dispatch_id)
    for job in pending:
        try:
            outcomes.append({"job_id": job["job_id"], **_run_job(dispatch_id, job)})
        except BusinessError as error:
            outcomes.append({"job_id": job["job_id"], "status": "failed", "error": error.code})
    return outcomes


def _fail(dispatch_id, job_id, reason):
    if not job_id:
        return
    with store.connection(immediate=True) as db:
        store.mark_job(db, dispatch_id, job_id, "failed", reason, _now())


# ---- 内部 -----------------------------------------------------------------

def _require(dispatch_id):
    with store.connection() as db:
        record = store.load_dispatch(db, dispatch_id)
    if record is None:
        raise BusinessError("UNKNOWN_DISPATCH", "找不到该下发批次", 404)
    return record


def _read_create(payload):
    if not isinstance(payload, dict):
        raise BusinessError("BAD_SOURCE", "数据须为 JSON 对象")
    if ("source" in payload) == ("sample" in payload):
        raise BusinessError("CHOOSE_SOURCE", "请选择一个样例或上传一份数据")
    if "sample" in payload:
        if not isinstance(payload["sample"], str):
            raise BusinessError("CHOOSE_SOURCE", "样例名称须为字符串")
        source = load_sample(payload["sample"])
    else:
        source = payload["source"]
    validate_source(source)
    batch = payload.get("batch", source.get("default_batch"))
    if batch is None:
        raise BusinessError("BAD_QUERY", "请指定召回批号")
    return source, normalize_query(batch, payload.get("start"), payload.get("end"))


def _counts(source):
    return {field: len(source.get(field) or []) for field in FIELDS}


def _query_of(record):
    return {"batch": record["batch"], "start": record["start"], "end": record["end"]}


def _task_basis(basis, task):
    """每件任务随身携带的下发依据，足够现场复核来源。"""
    return {
        "dispatch_id": basis["dispatch_id"],
        "revision_seq": basis["revision_seq"],
        "revision_id": basis["revision_id"],
        "previous_seq": basis["previous_seq"],
        "query": basis["query"],
        "window": basis["window"],
        "source_digest": basis["source_digest"],
        "rule_version": basis["rule_version"],
        "planner_version": basis["planner_version"],
        "input": basis["input"],
        "recall_count": basis["recall"]["count"],
        "exposures": task["exposures"],
    }


def _snapshot(dispatch_id, outcome):
    with store.connection() as db:
        record = store.load_dispatch(db, dispatch_id)
        latest = store.latest_revision(db, dispatch_id)
        if latest is None:
            raise BusinessError("NOT_DISPATCHED", "该批次还没有已下发的任务", 409)
        view = _revision_view(db, dispatch_id, latest)
        return {
            "dispatch_id": dispatch_id,
            "batch": record["batch"],
            "query": latest["basis"]["query"],
            "scope": latest["basis"].get("window"),
            "status": outcome.get("status", "ok"),
            "reused": bool(outcome.get("reused")),
            "job_id": outcome.get("job_id"),
            "error": outcome.get("error"),
            "resumed": outcome.get("resumed", []),
            "duplicate_supplement": bool(outcome.get("duplicate_supplement")),
            "supplement": outcome.get("supplement"),
            **view,
            "revisions": [_revision_head(row) for row in store.list_revisions(db, dispatch_id)],
            "inputs": store.list_inputs(db, dispatch_id),
            "jobs": store.jobs(db, dispatch_id),
        }


def _revision_head(row):
    return {
        "seq": row["seq"], "revision_id": row["revision_id"], "created": row["created"],
        "window": row["basis"].get("window"),
        "summary": row["plan"].get("summary", {}),
        "input": row["basis"].get("input"),
        "warnings": row["basis"].get("warnings", []),
    }


def _revision_view(db, dispatch_id, row):
    previous = store.get_revision(db, dispatch_id, row["seq"] - 1) if row["seq"] > 1 else None
    tasks = store.revision_tasks(db, dispatch_id, row["seq"])
    old_tasks = store.revision_tasks(db, dispatch_id, previous["seq"]) if previous else []
    delta = build_delta(old_tasks, tasks)
    return {
        "revision_seq": row["seq"],
        "revision_id": row["revision_id"],
        "created": row["created"],
        "window": row["basis"].get("window"),
        "basis": row["basis"],
        "summary": row["plan"].get("summary", {}),
        "warnings": row["basis"].get("warnings", []),
        "tasks": tasks,
        "delta": delta,
    }
