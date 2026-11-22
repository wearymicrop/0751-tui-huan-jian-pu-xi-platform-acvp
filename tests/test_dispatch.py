import pytest

from app.modules.dispatch import service, store
from app.source import load_sample


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAGE_DB", str(tmp_path / "dispatch.sqlite3"))


def _snapshot(dispatch_id):
    return service.detail(dispatch_id)


def test_dispatch_covers_every_recall_target(dirty_report):
    from app.analysis import with_recall

    recall = with_recall(dirty_report, "CU-041")["recall"]
    run = service.create({"sample": "dirty", "batch": "CU-041"})
    assert run["status"] == "applied" and run["revision_seq"] == 1
    assert sorted(task["serial"] for task in run["tasks"]) == recall["targets"]
    assert run["summary"]["tasks"] == recall["count"]
    assert {task["task_id"] for task in run["tasks"]}.__len__() == recall["count"]


def test_tasks_carry_dispatch_basis(dirty_report):
    run = service.create({"sample": "dirty", "batch": "CU-041"})
    for task in run["tasks"]:
        basis = task["basis"]
        assert basis["dispatch_id"] == run["dispatch_id"]
        assert basis["revision_seq"] == 1 and basis["revision_id"] == run["revision_id"]
        assert basis["query"]["batch"] == "CU-041"
        assert basis["window"] == run["window"]
        assert basis["source_digest"] == run["basis"]["source_digest"]
        assert basis["rule_version"] == "1.0"
        assert basis["exposures"] == task["exposures"]
        assert task["exposures"], task["serial"]


def test_action_follows_current_ownership(dirty_report):
    run = service.create({"sample": "dirty", "batch": "CU-041"})
    tasks = {task["serial"]: task for task in run["tasks"]}
    assert tasks["DRV-22"]["state"] == "mounted"
    assert tasks["DRV-22"]["action"] == "teardown_quarantine"
    assert tasks["UNIT-A"]["state"] == "stock"
    assert tasks["UNIT-A"]["action"] == "inspect_quarantine"
    assert tasks["DRV-21"]["state"] == "disputed"
    assert tasks["DRV-21"]["action"] == "hold_review"


def test_rerun_same_recall_does_not_redispatch(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    again = service.create({"sample": "dirty", "batch": "CU-041"})
    refreshed = service.refresh(first["dispatch_id"])
    assert again["dispatch_id"] == first["dispatch_id"]
    assert again["reused"] and refreshed["reused"]
    assert again["revision_seq"] == refreshed["revision_seq"] == 1
    assert {task["task_id"] for task in again["tasks"]} == {task["task_id"] for task in first["tasks"]}
    assert len({task["task_id"] for task in again["tasks"]}) == len(again["tasks"])


def test_late_record_withdraws_and_adds(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = first["dispatch_id"]
    before = {task["task_id"]: task for task in first["tasks"]}
    # 晚到的返库记录：DRV-22 在盘点前已被拆下并退回，现场不应再去柜组拆件。
    late = {"returns": [{"ticket": "RT-200", "serial": "DRV-22", "action": "return",
                         "at": "2026-05-13T08:00:00+08:00"}],
            "events": [{"child": "DRV-22", "parent": "UNIT-C", "action": "detach",
                        "revision": 2, "at": "2026-05-12T08:00:00+08:00"}]}
    second = service.supplement(dispatch_id, late)
    assert second["status"] == "applied" and second["revision_seq"] == 2
    after = {task["task_id"]: task for task in second["tasks"]}
    changed = next(task for task in second["tasks"] if task["serial"] == "DRV-22")
    assert changed["state"] == "returned"
    assert changed["action"] == "inspect_quarantine"
    withdrawn = {task["task_id"]: task for task in second["delta"]["withdrawn"]}
    added = {task["task_id"]: task for task in second["delta"]["added"]}
    assert withdrawn[next(key for key, task in before.items() if task["serial"] == "DRV-22")]
    assert added[changed["task_id"]]
    # 内容没变的任务原样保留，现场不会同时收到两个冲突版本。
    for serial in ("DRV-21", "UNIT-A"):
        kept = next(task for task in second["tasks"] if task["serial"] == serial)
        original = next(task for task in first["tasks"] if task["serial"] == serial)
        assert kept["task_id"] == original["task_id"]
    assert second["delta"]["counts"]["retained"] == 4


def test_supplement_does_not_freeze_initial_scope(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    # 晚到的同批触点，装进此前从未暴露的面板：影响面应当扩展到 PANEL-7。
    late = {
        "parts": [{"serial": "0010", "batch": "CU-041", "kind": "触点",
                   "registered": "2026-05-01T08:00:00+08:00"}],
        "events": [{"child": "0010", "parent": "PANEL-7", "action": "mount",
                    "revision": 1, "at": "2026-05-16T08:00:00+08:00"}],
    }
    second = service.supplement(first["dispatch_id"], late)
    assert "PANEL-7" in second["basis"]["recall"]["targets"]
    assert any(task["serial"] == "PANEL-7" for task in second["tasks"])
    assert "PANEL-7" not in first["basis"]["recall"]["targets"]


def test_duplicate_supplement_is_recorded_once(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    late = {"events": [{"child": "DRV-22", "parent": "UNIT-C", "action": "detach",
                        "revision": 2, "at": "2026-05-12T08:00:00+08:00"}]}
    service.supplement(first["dispatch_id"], late)
    repeat = service.supplement(first["dispatch_id"], late)
    assert repeat["reused"] and repeat["duplicate_supplement"]
    assert repeat["revision_seq"] == 2
    assert len([row for row in repeat["revisions"] if row["seq"] == 2]) == 1
    kinds = [row["kind"] for row in repeat["inputs"]]
    assert kinds.count("supplement") == 1 and kinds.count("duplicate_supplement") == 1


def test_identical_late_record_is_ignored(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    same = {"events": [load_sample("dirty")["events"][0]]}
    second = service.supplement(first["dispatch_id"], same)
    assert second["reused"] and second["revision_seq"] == 1
    assert second["supplement"]["duplicate"] >= 1 or second["supplement"] is None


def test_previous_revision_stays_traceable(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    late = {"returns": [{"ticket": "RT-201", "serial": "DRV-22", "action": "return",
                         "at": "2026-05-13T08:00:00+08:00"}]}
    service.supplement(first["dispatch_id"], late)
    old = service.revision(first["dispatch_id"], 1)
    assert old["revision_id"] == first["revision_id"]
    assert {task["task_id"] for task in old["tasks"]} == {task["task_id"] for task in first["tasks"]}


def test_failed_apply_keeps_old_tasks_and_resumes(dirty_report, monkeypatch):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = first["dispatch_id"]
    calls = {"n": 0}
    real = store.insert_tasks

    def boom(db, dispatch, seq, tasks):
        calls["n"] += 1
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "insert_tasks", boom)
    late = {"returns": [{"ticket": "RT-202", "serial": "DRV-22", "action": "return",
                         "at": "2026-05-13T08:00:00+08:00"}]}
    with pytest.raises(Exception) as error:
        service.supplement(dispatch_id, late)
    assert getattr(error.value, "code", None) == "DISPATCH_FAILED"
    # 旧任务仍在，可照常追溯
    still = _snapshot(dispatch_id)
    assert still["revision_seq"] == 1
    assert {task["task_id"] for task in still["tasks"]} == {task["task_id"] for task in first["tasks"]}
    failed = [job for job in still["jobs"] if job["status"] == "failed"]
    assert failed, still["jobs"]
    # 恢复写入后断点续跑，只落一版新修订
    monkeypatch.setattr(store, "insert_tasks", real)
    resumed = service.retry_job(dispatch_id, failed[0]["job_id"])
    assert resumed["revision_seq"] == 2 and resumed["delta"]["withdrawn"]
    assert calls["n"] == 1


def test_job_marked_superseded_cannot_be_retried(dirty_report, monkeypatch):
    from app.modules.dispatch import service as svc

    first = svc.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = first["dispatch_id"]
    real = store.insert_tasks
    state = {"fail": True}

    def flaky(db, dispatch, seq, tasks):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("transient")
        return real(db, dispatch, seq, tasks)

    monkeypatch.setattr(store, "insert_tasks", flaky)
    late_one = {"returns": [{"ticket": "RT-210", "serial": "DRV-22", "action": "return",
                             "at": "2026-05-13T08:00:00+08:00"}]}
    with pytest.raises(Exception):
        svc.supplement(dispatch_id, late_one)
    stale = next(job for job in _snapshot(dispatch_id)["jobs"] if job["status"] == "failed")
    # 下次进入会先续跑遗留作业；成功后它就不再是悬空作业。
    late_two = {"returns": [{"ticket": "RT-211", "serial": "UNIT-A", "action": "return",
                             "at": "2026-05-14T08:00:00+08:00"}]}
    latest = svc.supplement(dispatch_id, late_two)
    assert latest["revision_seq"] == 3
    status = {job["job_id"]: job["status"] for job in latest["jobs"]}
    assert status[stale["job_id"]] == "applied"
    assert any(row["job_id"] == stale["job_id"] and row["status"] == "applied"
               for row in latest["resumed"])
    assert latest["revision_seq"] == 3


def test_retry_rejects_superseded_job(dirty_report, monkeypatch):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = first["dispatch_id"]
    real = store.insert_tasks
    state = {"fail": True}

    def flaky(db, dispatch, seq, tasks):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("transient")
        return real(db, dispatch, seq, tasks)

    monkeypatch.setattr(store, "insert_tasks", flaky)
    late = {"returns": [{"ticket": "RT-220", "serial": "DRV-22", "action": "return",
                         "at": "2026-05-13T08:00:00+08:00"}]}
    with pytest.raises(Exception):
        service.supplement(dispatch_id, late)
    failed = next(job for job in _snapshot(dispatch_id)["jobs"] if job["status"] == "failed")
    with store.connection() as db:
        stored = store.load_job(db, dispatch_id, failed["job_id"])
    assert stored["payload"] is not None
    # 人为把作业标成过时，验证现场不会续跑被取代的版本（外部调用了 cancel 语义）
    with store.connection(immediate=True) as db:
        store.mark_job(db, dispatch_id, failed["job_id"], "superseded", None, "2026-01-01T00:00:00Z")
    with pytest.raises(Exception) as error:
        service.retry_job(dispatch_id, failed["job_id"])
    assert getattr(error.value, "code", None) == "STALE_JOB"


def test_preview_does_not_persist(dirty_report):
    preview = service.preview(load_sample("dirty"), "CU-041")
    assert preview["persisted"] is False
    assert preview["existing"] is None
    assert preview["summary"]["tasks"] == 5
    assert service.runs()["dispatches"] == []
    assert preview["dispatch_id"] == service.create({"sample": "dirty", "batch": "CU-041"})["dispatch_id"]


def test_dispatch_window_partitions_recall(client):
    run = client.post("/api/dispatches", json={
        "sample": "dirty", "batch": "CU-041",
        "start": "2026-05-05T00:00:00Z", "end": "2026-05-06T00:00:00Z",
    }).json()
    assert [task["serial"] for task in run["tasks"]] == ["DRV-21", "UNIT-B"]
    assert run["window"] == {"start": "2026-05-05T00:00:00.000000Z",
                             "end": "2026-05-06T00:00:00.000000Z"}


def test_http_dispatch_flow(client):
    created = client.post("/api/dispatches", json={"sample": "dirty", "batch": "CU-041"})
    assert created.status_code == 200
    run = created.json()
    again = client.post("/api/dispatches", json={"sample": "dirty", "batch": "CU-041"}).json()
    assert again["reused"] and again["revision_seq"] == 1
    detail = client.get(f"/api/dispatches/{run['dispatch_id']}").json()
    assert len(detail["tasks"]) == 5 and detail["basis"]["rule_version"] == "1.0"
    late = client.post(f"/api/dispatches/{run['dispatch_id']}/supplements", json={
        "returns": [{"ticket": "RT-300", "serial": "DRV-22", "action": "return",
                     "at": "2026-05-13T08:00:00+08:00"}]}).json()
    assert late["revision_seq"] == 2 and late["delta"]["counts"]["withdrawn"] >= 1
    old = client.get(f"/api/dispatches/{run['dispatch_id']}/revisions/1").json()
    assert old["revision_id"] == run["revision_id"]
    listed = client.get("/api/dispatches").json()["dispatches"]
    assert [row["id"] for row in listed] == [run["dispatch_id"]]
    assert client.get("/api/dispatches/D-missing").json()["code"] == "UNKNOWN_DISPATCH"


def test_bad_supplement_rejected(client):
    run = client.post("/api/dispatches", json={"sample": "dirty", "batch": "CU-041"}).json()
    reply = client.post(f"/api/dispatches/{run['dispatch_id']}/supplements", json={"events": "x"})
    assert (reply.status_code, reply.json()["code"]) == (422, "BAD_SOURCE")
    reply = client.post(f"/api/dispatches/{run['dispatch_id']}/supplements", json={"note": 1})
    assert (reply.status_code, reply.json()["code"]) == (422, "BAD_SOURCE")


def test_concurrent_rerun_dispatches_once(dirty_report):
    from concurrent.futures import ThreadPoolExecutor

    def create(_):
        return service.create({"sample": "dirty", "batch": "CU-041"})

    with ThreadPoolExecutor(max_workers=6) as workers:
        runs = list(workers.map(create, range(6)))
    ids = {run["dispatch_id"] for run in runs}
    assert len(ids) == 1
    final = service.detail(ids.pop())
    assert final["revision_seq"] == 1
    assert len(final["revisions"]) == 1
    assert len({task["task_id"] for task in final["tasks"]}) == final["summary"]["tasks"]
    assert all(job["status"] != "failed" for job in final["jobs"])


def test_concurrent_supplements_keep_single_chain(dirty_report):
    from concurrent.futures import ThreadPoolExecutor

    run = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = run["dispatch_id"]
    payloads = [
        {"returns": [{"ticket": f"RT-40{i}", "serial": serial, "action": "return",
                      "at": "2026-05-13T08:00:00+08:00"}]}
        for i, serial in enumerate(("DRV-22", "0007", "UNIT-A"))
    ]

    def add(payload):
        try:
            return service.supplement(dispatch_id, payload)["revision_seq"]
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(add, payloads))
    final = service.detail(dispatch_id)
    seqs = [revision["seq"] for revision in final["revisions"]]
    assert seqs == list(range(len(seqs), 0, -1)), seqs
    assert len(seqs) == len(set(seqs))
    assert all(job["status"] == "applied" for job in final["jobs"]), final["jobs"]
    assert len({task["task_id"] for task in final["tasks"]}) == final["summary"]["tasks"]


def test_failed_supplement_stays_in_job_for_later_merge(dirty_report, monkeypatch):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = first["dispatch_id"]
    real = store.insert_tasks
    state = {"fail": True}

    def flaky(db, dispatch, seq, tasks):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("disk full")
        return real(db, dispatch, seq, tasks)

    monkeypatch.setattr(store, "insert_tasks", flaky)
    late = {
        "parts": [{"serial": "0011", "batch": "CU-041", "kind": "触点",
                   "registered": "2026-05-01T08:00:00+08:00"}],
        "events": [{"child": "0011", "parent": "PANEL-7", "action": "mount",
                    "revision": 1, "at": "2026-05-16T08:00:00+08:00"}],
    }
    with pytest.raises(Exception):
        service.supplement(dispatch_id, late)
    broken = _snapshot(dispatch_id)
    failed = next(job for job in broken["jobs"] if job["status"] == "failed")
    with store.connection() as db:
        stored = store.load_job(db, dispatch_id, failed["job_id"])
    assert stored["payload"] == late
    # 源数据仍是失败前那一份，旧任务可原样追溯
    assert broken["revision_seq"] == 1
    other = {"returns": [{"ticket": "RT-501", "serial": "UNIT-A", "action": "return",
                          "at": "2026-05-14T08:00:00+08:00"}]}
    resumed = service.supplement(dispatch_id, other)
    assert resumed["revision_seq"] == 3
    assert any(row["job_id"] == failed["job_id"] and row["status"] == "applied"
               for row in resumed["resumed"])
    tasks = {task["serial"]: task for task in resumed["tasks"]}
    assert "PANEL-7" in tasks and tasks["UNIT-A"]["state"] == "returned"
    assert all(job["status"] != "failed" for job in resumed["jobs"])


def test_resubmit_with_late_rows_merges_instead_of_overwriting(dirty_report):
    first = service.create({"sample": "dirty", "batch": "CU-041"})
    data = load_sample("dirty")
    data["parts"].append({"serial": "0012", "batch": "CU-041", "kind": "触点",
                          "registered": "2026-05-01T08:00:00+08:00"})
    data["events"].append({"child": "0012", "parent": "PANEL-7", "action": "mount",
                           "revision": 1, "at": "2026-05-17T08:00:00+08:00"})
    second = service.create({"source": data, "batch": "CU-041"})
    assert second["dispatch_id"] == first["dispatch_id"]
    assert second["revision_seq"] == 2
    assert "PANEL-7" in second["basis"]["recall"]["targets"]
    with store.connection() as db:
        stored = store.load_dispatch(db, first["dispatch_id"])
    assert sum(1 for row in stored["source"]["parts"] if row["serial"] == "0012") == 1
    assert service.create({"source": data, "batch": "CU-041"})["reused"]


def test_concurrent_ledger_entries_survive(dirty_report):
    from concurrent.futures import ThreadPoolExecutor

    run = service.create({"sample": "dirty", "batch": "CU-041"})
    dispatch_id = run["dispatch_id"]
    payloads = [
        {"returns": [{"ticket": f"RT-60{i}", "serial": serial, "action": "return",
                      "at": "2026-05-14T08:00:00+08:00"}]}
        for i, serial in enumerate(("DRV-22", "0008", "UNIT-A"))
    ]
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(lambda payload: _quiet(service.supplement, dispatch_id, payload), payloads))
    final = service.detail(dispatch_id)
    with store.connection() as db:
        stored = store.load_dispatch(db, dispatch_id)
    tickets = {row["ticket"] for row in stored["source"]["returns"]}
    assert {f"RT-60{i}" for i in range(3)} <= tickets
    assert all(job["status"] != "failed" for job in final["jobs"]), final["jobs"]
    assert len(final["revisions"]) == 4  # 1 次首次 + 3 次补录


def _quiet(function, *args):
    try:
        return function(*args)
    except Exception:
        return None


def test_resume_before_first_revision_is_written(dirty_report, monkeypatch):
    real = store.insert_revision

    def boom(db, *args, **kwargs):
        raise RuntimeError("crash before revision")

    monkeypatch.setattr(store, "insert_revision", boom)
    with pytest.raises(Exception) as error:
        service.create({"sample": "dirty", "batch": "CU-041"})
    assert getattr(error.value, "code", None) == "DISPATCH_FAILED"
    listed = service.runs()["dispatches"]
    assert len(listed) == 1 and listed[0]["seq"] == 0
    monkeypatch.setattr(store, "insert_revision", real)
    # 新进程里的下一次进入会先续跑遗留作业，只落一版；本次重算直接复用。
    resumed = service.refresh(listed[0]["id"])
    assert resumed["revision_seq"] == 1 and resumed["reused"]
    assert resumed["resumed"][0]["status"] == "applied"
    assert len(resumed["tasks"]) == 5
    assert len(service.detail(listed[0]["id"])["revisions"]) == 1
