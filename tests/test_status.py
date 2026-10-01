"""本轮功能测试: 评审资格停用/启用 (会务方按评审人编号调整资格)。

覆盖需求:
- 既有评审人初始视为启用; 会务方凭密钥、评审人编号、所见资料修订号、
  目标状态与非空原因调整资格, 并可查看变更记录 (按人/全局);
- 停用后现有凭据立即不能取稿、提交决定或评语 (凭据仍识别 -> 403);
  既有发布分配、确认和评语不删除; 已发布作者反馈快照仍按原规则有效;
- 停用者不进入后续普通分配与补位; 补位释放其已确认槽位,
  其他仍合格的已确认槽位保持固定;
- 启用后只恢复当前发布版中仍分配给本人的任务访问,
  不找回已被补位替换的槽位或归档评语;
- 状态变更同一事务内先核对修订号: 过期修订号 409、未知评审人 404、空原因 422,
  拒绝均不改状态; 匹配修订号下重复提交同一状态和原因幂等不推进修订号,
  同状态异原因 409; 有效变更推进修订号, 基于旧修订号的发布/补位发布 409;
- 现有凭据格式与评审接口约定保持兼容。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-status-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals",
            "submitted_reviews", "feedback_snapshots",
            "reviewer_status_changes",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def set_status(client, rid, revision, status, reason):
    return client.put(
        f"/reviewers/{rid}/status",
        headers=ORG,
        json={"base_revision": revision, "status": status, "reason": reason},
    )


def setup_published(client, reviewers=("R1", "R2", "R3", "R4"), papers=("P1",)):
    for rid in reviewers:
        add_reviewer(client, rid, f"Inst-{rid}")
    for pid in papers:
        add_paper(client, pid, institutions=[f"Author-{pid}"])
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["plan"], rev + 0


def decide(client, rid, pid, decision, reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


# ------------------------------------------------------------ 初始状态与基本校验

def test_existing_reviewers_initially_enabled(client):
    add_reviewer(client, "R1", "Inst-A")
    assert client.get("/reviewers", headers=ORG).json()["reviewers"][0]["status"] == "enabled"
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["status"] == "enabled" and st["changes"] == []


def test_disable_then_enable_valid_changes_bump_revision_and_log(client):
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    r = set_status(client, "R1", rev0, "disabled", "违规联系作者")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["status"] == "disabled"
    assert body["revision"] == rev0 + 1
    rev1 = body["revision"]
    # 变更记录可查 (按人)
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["status"] == "disabled"
    assert st["changes"] == [{
        "reviewer_id": "R1", "status": "disabled", "reason": "违规联系作者",
        "revision": rev1, "changed_at": st["changes"][0]["changed_at"],
    }]
    # 再启用: 又是有效变更, 再推进修订号并追加记录
    r = set_status(client, "R1", rev1, "enabled", "申诉成立恢复资格")
    assert r.json()["changed"] is True
    rev2 = r.json()["revision"]
    assert rev2 == rev1 + 1
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["status"] == "enabled" and [c["status"] for c in st["changes"]] == ["disabled", "enabled"]
    # 全局变更记录
    changes = client.get("/reviewer-status-changes", headers=ORG).json()["changes"]
    assert [c["status"] for c in changes] == ["disabled", "enabled"]
    assert client.get(
        "/reviewer-status-changes", headers=ORG, params={"reviewer_id": "R1"}
    ).json()["changes"] == st["changes"]
    assert client.get(
        "/reviewer-status-changes", headers=ORG, params={"reviewer_id": "NOPE"}
    ).json()["changes"] == []


def test_stale_revision_rejected_without_change(client):
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    add_paper(client, "P1", institutions=["X"])  # 资料变更 -> 修订号前进
    r = set_status(client, "R1", rev0, "disabled", "原因")
    assert r.status_code == 409
    # 状态未变, 无记录
    assert client.get("/reviewers/R1/status", headers=ORG).json()["status"] == "enabled"
    assert client.get("/reviewer-status-changes", headers=ORG).json()["changes"] == []
    assert client.get("/meta", headers=ORG).json()["revision"] == rev0 + 1


def test_unknown_reviewer_status_endpoints_404(client):
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    assert set_status(client, "GHOST", rev0, "disabled", "x").status_code == 404
    assert client.get("/reviewers/GHOST/status", headers=ORG).status_code == 404


def test_unknown_reviewer_checked_after_revision_in_same_txn(client):
    """同一事务内先核对修订号: 修订号过期时即使评审人未知也返回 409。"""
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    add_paper(client, "P1", institutions=["X"])
    r = set_status(client, "GHOST", rev0, "disabled", "x")
    assert r.status_code == 409  # 过期修订号先于未知评审人被拒


def test_empty_reason_rejected_422(client):
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    for reason in ("", "   ", "\t\n"):
        r = set_status(client, "R1", rev0, "disabled", reason)
        # 纯空白在 schema 层 (min_length) 或接口层都应是 422
        assert r.status_code == 422
    # 缺字段同样 422
    r = client.put(
        "/reviewers/R1/status", headers=ORG,
        json={"base_revision": rev0, "status": "disabled"},
    )
    assert r.status_code == 422
    assert client.get("/reviewers/R1/status", headers=ORG).json()["status"] == "enabled"


def test_duplicate_same_status_same_reason_idempotent_no_bump(client):
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    r1 = set_status(client, "R1", rev0, "disabled", "原因甲")
    rev1 = r1.json()["revision"]
    r2 = set_status(client, "R1", rev1, "disabled", "原因甲")
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == rev1
    assert r2.json()["reason"] == "原因甲"
    # 只有一条变更记录
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert len(st["changes"]) == 1


def test_same_status_different_reason_rejected_409(client):
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    rev1 = set_status(client, "R1", rev0, "disabled", "原因甲").json()["revision"]
    r = set_status(client, "R1", rev1, "disabled", "原因乙")
    assert r.status_code == 409
    # 状态与记录均不变
    st = client.get("/reviewers/R1/status", headers=ORG).json()
    assert st["status"] == "disabled"
    assert len(st["changes"]) == 1 and st["changes"][0]["reason"] == "原因甲"
    # 修订号不前进
    assert client.get("/meta", headers=ORG).json()["revision"] == rev1


def test_enable_on_never_changed_reviewer_is_idempotent_noop(client):
    """既有评审人初始视为启用: 对其提交 enabled 是幂等空操作, changed=false。"""
    add_reviewer(client, "R1", "Inst-A")
    rev0 = client.get("/meta", headers=ORG).json()["revision"]
    r = set_status(client, "R1", rev0, "enabled", "保持启用")
    assert r.status_code == 200
    assert r.json()["changed"] is False and r.json()["status"] == "enabled"
    assert r.json()["revision"] == rev0
    assert client.get("/reviewers/R1/status", headers=ORG).json()["changes"] == []
    assert client.get("/meta", headers=ORG).json()["revision"] == rev0


def test_status_endpoint_requires_organizer_key(client):
    assert client.put(
        "/reviewers/R1/status",
        json={"base_revision": 0, "status": "disabled", "reason": "x"},
    ).status_code == 401
    assert client.get("/reviewer-status-changes").status_code == 401


# ------------------------------------------------------------ 停用后凭据立即失效

def test_disabled_reviewer_credentials_immediately_rejected(client):
    plan, _ = setup_published(client)
    rid = plan["P1"][0]
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid, rev, "disabled", "暂时停职")
    # 现有凭据立即不能取稿、提交决定或评语
    r = client.get("/reviewer/assignments", headers=reviewer_headers(rid))
    assert r.status_code == 403
    r = decide(client, rid, "P1", "confirm")
    assert r.status_code == 403
    r = client.post(
        "/reviewer/assignments/P1/review",
        headers=reviewer_headers(rid),
        json={"paper_id": "P1", "serial": 1, "score": 4, "comment": "评语"},
    )
    assert r.status_code == 403
    r = client.get("/reviewer/reviews", headers=reviewer_headers(rid))
    assert r.status_code == 403
    # 凭据错误仍然是 401 (格式与约定保持兼容)
    r = client.get(
        "/reviewer/assignments",
        headers={"X-Reviewer-Id": rid, "X-Reviewer-Credential": "wrong"},
    )
    assert r.status_code == 401


def test_disabling_keeps_published_assignment_decision_and_review(client):
    """停用不删除既有发布分配、确认与评语; 另一评审人不受影响。"""
    plan, _ = setup_published(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    rev_before_disable = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid_a, rev_before_disable, "disabled", "原因")
    # 当前发布方案原样保留 (含 rid_a 槽位与其确认状态)
    pub = client.get("/assignment", headers=ORG).json()
    assert set(pub["plan"]["P1"]) == {rid_a, rid_b}
    states = {s["reviewer_id"]: s["state"] for s in pub["papers"]["P1"]["slots"]}
    assert states == {rid_a: "confirmed", rid_b: "confirmed"}
    # 另一合格评审人照常取稿
    view_b = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).json()
    assert [p["paper_id"] for p in view_b["assignments"]] == ["P1"]
    # 停用者出现在 /meta 清单
    assert client.get("/meta", headers=ORG).json()["disabled_reviewers"] == [rid_a]


def test_published_feedback_snapshot_still_valid_after_disabling(client):
    plan, _ = setup_published(client)
    rid_a, rid_b = plan["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    client.post(
        "/reviewer/assignments/P1/review",
        headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": serial, "score": 4, "comment": "评语甲"},
    )
    client.post(
        "/reviewer/assignments/P1/review",
        headers=reviewer_headers(rid_b),
        json={"paper_id": "P1", "serial": serial, "score": 2, "comment": "评语乙"},
    )
    snap = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    code = snap["access_code"]
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid_a, rev, "disabled", "原因")
    # 已发布的作者反馈快照仍按原有规则有效 (无需凭据可读, 且不暴露评审人编号)
    r = client.get(f"/feedback-snapshots/{code}")
    assert r.status_code == 200
    body = r.json()
    assert body["paper_id"] == "P1"
    assert [x["score"] for x in body["reviews"]] == [4, 2]


# ------------------------------------------------------------ 停用者不进入后续分配/补位

def test_disabled_reviewer_excluded_from_new_assignment(client):
    add_reviewer(client, "R1", "Inst-1")
    add_reviewer(client, "R2", "Inst-2")
    add_reviewer(client, "R3", "Inst-3")
    add_paper(client, "P1", institutions=["Author-A"])
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R1", rev, "disabled", "原因")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert "R1" not in body["plan"]["P1"]
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    assert reasons["R1"] == "reviewer_disabled"
    # 停用不占容量: loads 中 R1 为 0
    assert body["loads"]["R1"] == 0


def test_disable_then_backfill_releases_confirmed_slot_keeps_others(client):
    # 两篇论文: 停用某评审人后补位, 其已确认槽位释放, 其他已确认槽位固定
    plan, _ = setup_published(
        client,
        reviewers=("R1", "R2", "R3", "R4"),
        papers=("P1", "P2"),
    )
    # 找到同属 P1、P2 的评审人 (容量 3 下按字典序最小方案通常 R1/R2 跨两篇)
    # 让 R1 对 P1、P2 都确认; 再停用 R1, 补位应释放两篇上 R1 的全部槽位
    count_before = sum(1 for pair in plan.values() if "R1" in pair)
    assert count_before >= 1
    for pid, pair in plan.items():
        if "R1" in pair:
            decide(client, "R1", pid, "confirm")
    # 另一名评审人在其中一篇上确认且不停用 -> 该槽位保持固定
    fixed_pid = next(pid for pid, pair in plan.items() if "R1" in pair)
    other = next(rid for rid in plan[fixed_pid] if rid != "R1")
    if other != "R1":
        decide(client, other, fixed_pid, "confirm")

    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R1", rev, "disabled", "原因")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    for pid, pair in dry["plan"].items():
        assert "R1" not in pair
    # R1 的已确认槽位被释放并在 fixed_problems 中标注
    problems = {
        (pid, p["reviewer_id"], p["reason"])
        for pid, ps in dry["fixed_problems"].items()
        for p in ps
    }
    for pid in plan:
        if "R1" in plan[pid]:
            assert (pid, "R1", "reviewer_disabled") in problems
    # 其他仍合格的已确认槽位保持固定
    assert other in dry["fixed"][fixed_pid]

    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    new_plan = r.json()["plan"]
    assert all("R1" not in pair for pair in new_plan.values())
    assert other in new_plan[fixed_pid]
    # R1 被补位移出后仍然停用; 其他固定者确认状态保留
    view = client.get("/reviewer/assignments", headers=reviewer_headers(other)).json()
    assert fixed_pid in [p["paper_id"] for p in view["assignments"]]
    assert view["states"][fixed_pid] == "confirmed"


def test_disable_is_a_revision_change_old_publish_base_rejected(client):
    """有效变更推进修订号: 基于旧修订号的普通发布与补位发布均 409。"""
    plan, _ = setup_published(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "recuse", reason="冲突")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    rev, serial = dry["revision"], dry["serial"]
    # 停用另一名评审人 -> 资料修订号前进
    other = next(x for x in ("R1", "R2", "R3", "R4") if x not in plan["P1"])
    set_status(client, other, rev, "disabled", "原因")
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": rev, "base_serial": serial},
    )
    assert r.status_code == 409
    # 普通发布同理
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).status_code == 409


# ------------------------------------------------------------ 启用恢复语义

def test_enable_restores_only_current_published_tasks(client):
    """停用 -> 补位替换其槽位 -> 启用: 只恢复当前发布版仍分给本人的任务。"""
    plan, _ = setup_published(client)
    rid_a, rid_b = plan["P1"]
    # rid_a 先确认, rid_b 待停用; 补位用 R3/R4 之一替换 rid_b
    decide(client, rid_a, "P1", "confirm")
    serial0 = client.get("/assignment", headers=ORG).json()["serial"]
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid_b, rev, "disabled", "临时停职")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200
    replacement = next(x for x in r.json()["plan"]["P1"] if x != rid_a)
    assert replacement != rid_b
    # 停用期间凭据拒绝
    assert client.get(
        "/reviewer/assignments", headers=reviewer_headers(rid_b)
    ).status_code == 403
    # 重新启用
    rev2 = client.get("/meta", headers=ORG).json()["revision"]
    r = set_status(client, rid_b, rev2, "enabled", "调查结束恢复")
    assert r.status_code == 200 and r.json()["changed"] is True
    # 启用后不找回已被补位替换的槽位: 当前无任务分给 rid_b
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid_b)).json()
    assert view["serial"] == serial0 + 1
    assert view["assignments"] == []
    # 历史槽位也不在本人当前评语视图
    assert client.get(
        "/reviewer/reviews", headers=reviewer_headers(rid_b)
    ).json()["reviews"] == []


def test_enable_keeps_existing_decision_when_slot_still_assigned(client):
    """停用后未发生补位 (槽位仍在当前发布版): 启用恢复访问, 原决定记录仍在。"""
    plan, _ = setup_published(client)
    rid = plan["P1"][0]
    decide(client, rid, "P1", "confirm")
    rev1 = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid, rev1, "disabled", "临时停职")
    rev2 = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, rid, rev2, "enabled", "恢复")
    view = client.get("/reviewer/assignments", headers=reviewer_headers(rid)).json()
    assert [p["paper_id"] for p in view["assignments"]] == ["P1"]
    assert view["states"]["P1"] == "confirmed"


def test_disabled_reviewer_can_still_be_modified_or_deleted_by_organizer(client):
    add_reviewer(client, "R1", "Inst-A")
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R1", rev, "disabled", "原因")
    # 资料维护不受资格状态影响 (凭据格式保持兼容)
    r = client.put(
        "/reviewers/R1", headers=ORG,
        json={
            "credential": "cred-R1", "topics": ["AI"], "institution": "Inst-A",
            "capacity": 5, "avoid_papers": [],
        },
    )
    assert r.status_code == 200
    assert client.get("/reviewers/R1/status", headers=ORG).json()["status"] == "disabled"


def test_reviewer_status_change_logs_ordered_and_filterable(client):
    for rid in ("R1", "R2"):
        add_reviewer(client, rid, f"Inst-{rid}")
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R1", rev, "disabled", "d1")
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R2", rev, "disabled", "d2")
    rev = client.get("/meta", headers=ORG).json()["revision"]
    set_status(client, "R1", rev, "enabled", "e1")
    changes = client.get("/reviewer-status-changes", headers=ORG).json()["changes"]
    assert [(c["reviewer_id"], c["status"]) for c in changes] == [
        ("R1", "disabled"), ("R2", "disabled"), ("R1", "enabled"),
    ]
    # 修订号随每条记录单调递增
    assert [c["revision"] for c in changes] == sorted(c["revision"] for c in changes)
