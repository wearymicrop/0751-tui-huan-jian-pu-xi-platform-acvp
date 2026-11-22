from fastapi import APIRouter, Body

from app.errors import BusinessError
from app.modules.dispatch import service
from app.source import load_sample

router = APIRouter()


@router.get("/api/dispatches")
def list_dispatches():
    return service.runs()


@router.post("/api/dispatches/preview")
def preview_dispatch(payload: object = Body(...)):
    """只计算并返回任务清单，不落库、不派单。"""
    if not isinstance(payload, dict):
        raise BusinessError("BAD_SOURCE", "数据须为 JSON 对象")
    if ("source" in payload) == ("sample" in payload):
        raise BusinessError("CHOOSE_SOURCE", "请选择一个样例或上传一份数据")
    source = load_sample(payload["sample"]) if "sample" in payload else payload["source"]
    batch = payload.get("batch", source.get("default_batch") if isinstance(source, dict) else None)
    if batch is None:
        raise BusinessError("BAD_QUERY", "请指定召回批号")
    return service.preview(source, batch, payload.get("start"), payload.get("end"))


@router.post("/api/dispatches")
def create_dispatch(payload: object = Body(...)):
    return service.create(payload)


@router.get("/api/dispatches/{dispatch_id}")
def dispatch_detail(dispatch_id: str):
    return service.detail(dispatch_id)


@router.post("/api/dispatches/{dispatch_id}/refresh")
def refresh_dispatch(dispatch_id: str):
    return service.refresh(dispatch_id)


@router.post("/api/dispatches/{dispatch_id}/supplements")
def add_supplement(dispatch_id: str, payload: object = Body(...)):
    return service.supplement(dispatch_id, payload)


@router.get("/api/dispatches/{dispatch_id}/revisions/{seq}")
def revision_detail(dispatch_id: str, seq: int):
    return service.revision(dispatch_id, seq)


@router.post("/api/dispatches/{dispatch_id}/jobs/{job_id}/retry")
def retry_job(dispatch_id: str, job_id: str):
    return service.retry_job(dispatch_id, job_id)
