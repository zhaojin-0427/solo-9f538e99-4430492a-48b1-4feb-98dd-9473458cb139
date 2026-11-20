"""潮段：拐点识别、断点、方向与潮差、范围查询、持久化与增量重算。"""

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)

MIN = 60 * 1000


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM tide_segments")
    yield


# ---------------------------------------------------------------- 工具

def calibrate(segment, pairs):
    r = client.post("/calibrations", json={"calibrations": [
        {"boot_segment": segment, "counter": c, "utc_ms": t} for c, t in pairs
    ]})
    assert r.status_code == 200, r.text
    return r.json()


def anchor(segment="A", span=10_000_000):
    """让计数与 UTC 毫秒 1:1，方便构造读数。"""
    return calibrate(segment, [(0, 0), (span, span)])


def post_obs(segment, readings):
    """readings: [(counter, tide_level)]，观测 ID 依次为 {段}-o0、o1……"""
    items = [
        {"obs_id": f"{segment}-o{i}", "boot_segment": segment,
         "counter": c, "tide_level": lvl}
        for i, (c, lvl) in enumerate(readings)
    ]
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == 200, r.text
    return r.json()


def query(start, end):
    r = client.get("/tide-segments", params={"start_ms": start, "end_ms": end})
    assert r.status_code == 200, r.text
    return r.json()


def segs(start=0, end=10_000_000):
    return query(start, end)["items"]


def endpoints(body):
    return [(s["start_obs_id"], s["end_obs_id"]) for s in body["segments_added"]]


# ---------------------------------------------------------------- 基本形状

def test_rising_falling_segments_with_direction_and_range():
    anchor()
    body = post_obs("A", [(0, 0.5), (1000, 3.0), (2000, 0.2), (3000, 2.8), (4000, 0.4)])
    assert endpoints(body) == [("A-o1", "A-o2"), ("A-o2", "A-o3")]
    added = body["segments_added"]
    assert {
        "boot_segment": "A", "direction": "falling",
        "start_utc_ms": 1000, "start_tide_level": 3.0, "start_obs_id": "A-o1",
        "end_utc_ms": 2000, "end_tide_level": 0.2, "end_obs_id": "A-o2",
    }.items() <= added[0].items()
    assert added[0]["tidal_range"] == pytest.approx(2.8)
    assert added[1]["direction"] == "rising"
    assert added[1]["start_utc_ms"] == 2000 and added[1]["end_utc_ms"] == 3000
    assert added[1]["tidal_range"] == pytest.approx(2.6)

    items = segs()
    assert [s["direction"] for s in items] == ["falling", "rising"]
    assert items[0]["start_obs_id"] == "A-o1"
    assert items[0]["end_utc_ms"] == 2000

    # 一条完整的查询记录包含开机段、方向、两端时刻/潮位/ID 与潮差
    assert set(items[0]) >= {
        "boot_segment", "direction", "start_utc_ms", "start_tide_level",
        "start_obs_id", "end_utc_ms", "end_tide_level", "end_obs_id", "tidal_range",
    }


def test_two_boot_segments_are_computed_independently():
    anchor("A")
    # B 段整体平移，避免两段读数落在同一 UTC 毫秒而触发冲突
    calibrate("B", [(0, 100_000), (10_000_000, 10_100_000)])
    post_obs("A", [(0, 1.0), (1000, 3.0), (2000, 1.0), (3000, 2.0)])
    post_obs("B", [(0, 1.0), (1000, 5.0), (2000, 1.0), (3000, 4.0)])
    items = segs()
    assert {s["boot_segment"] for s in items} == {"A", "B"}
    assert len([s for s in items if s["boot_segment"] == "A"]) == 1
    assert len([s for s in items if s["boot_segment"] == "B"]) == 1


# ---------------------------------------------------------------- 拐点规则

def test_plateau_counts_as_single_point_with_first_time_and_id():
    anchor()
    # 2.0 连续三次；高点只认首条（t=1000，ID 最小）
    post_obs("A", [
        (0, 1.0), (1000, 2.0), (2000, 2.0), (3000, 2.0), (4000, 0.5), (5000, 1.5),
    ])
    items = segs()
    assert len(items) == 1
    seg = items[0]
    assert (seg["start_utc_ms"], seg["start_obs_id"]) == (1000, "A-o1")
    assert (seg["end_utc_ms"], seg["end_obs_id"]) == (4000, "A-o4")
    assert seg["direction"] == "falling"


def test_same_ms_same_value_uses_smallest_obs_id():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    # 同一毫秒同值，ID 更小的一条应成为拐点
    r = client.post("/observations", json={"observations": [
        {"obs_id": "A-extra", "boot_segment": "A", "counter": 1000, "tide_level": 2.0},
    ]})
    assert r.status_code == 200
    items = segs()
    assert items[0]["start_obs_id"] == "A-extra"  # "A-extra" < "A-o1"
    assert items[0]["start_utc_ms"] == 1000


def test_same_ms_different_values_breaks_timeline():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (4000, 1.5)])
    assert len(segs()) == 1  # 1000 高点 -> 2000 低点

    # t=3000 加两条不同潮位的读数 -> 该时刻断开，但 1000/2000 仍可成段
    r = client.post("/observations", json={"observations": [
        {"obs_id": "A-x", "boot_segment": "A", "counter": 3000, "tide_level": 4.0},
        {"obs_id": "A-y", "boot_segment": "A", "counter": 3000, "tide_level": 5.0},
    ]})
    assert r.status_code == 200
    # 断点把 2000 低点隔离成片段末点，无法再确认拐点 -> 旧段撤下
    assert segs() == []

    # 移除同刻异值中的一条，时刻恢复可信 -> 新段列入
    body = client.post("/observations", json={"observations": [
        {"obs_id": "A-y", "boot_segment": "A", "counter": 3000, "tide_level": 4.0},
    ]}).json()
    # 1000->2000 与 2000->3000 两段重新成立
    assert body["segments_added_count"] == 2


def test_non_finite_level_breaks_timeline():
    anchor()
    raw = json.dumps({"observations": [
        {"obs_id": "A-o0", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "A-o1", "boot_segment": "A", "counter": 1000, "tide_level": 2.0},
        {"obs_id": "A-bad", "boot_segment": "A", "counter": 2000,
         "tide_level": float("inf")},
        {"obs_id": "A-o3", "boot_segment": "A", "counter": 3000, "tide_level": 0.5},
        {"obs_id": "A-o4", "boot_segment": "A", "counter": 4000, "tide_level": 1.5},
    ]})
    r = client.post("/observations", content=raw,
                    headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text
    # 断点把高/低点拆到两侧，两侧都凑不齐一对拐点
    assert segs() == []


def test_conflict_breaks_timeline_and_resolves_on_recalibration():
    anchor("A")
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 2.0)])
    assert len(segs()) == 1

    # B 段一条读数落在 t=2000，与 A 冲突 -> A 的 t=2000 时刻断开
    calibrate("B", [(0, 0), (5000, 5000)])
    post_obs("B", [(2000, 7.0)])
    assert client.get("/observations/A-o2").json()["status"] == "conflict"
    # 低点时刻因冲突断开，夹在断点两侧的点无法连成潮段
    assert segs() == []

    # 更正 B 段校时点解除冲突 -> 潮段恢复，并在回执中列入
    body = calibrate("B", [(2000, 2500)])
    assert ("A-o1", "A-o2") in endpoints(body)
    assert len(segs()) == 1


def test_gap_of_exactly_30_minutes_connects():
    anchor()
    low_at = 1000 + 30 * MIN
    post_obs("A", [(0, 1.0), (1000, 2.0), (low_at, 0.5), (low_at + 1000, 2.0)])
    items = segs()
    assert len(items) == 1
    assert (items[0]["start_utc_ms"], items[0]["end_utc_ms"]) == (1000, low_at)


def test_gap_over_30_minutes_breaks():
    anchor()
    low_at = 1000 + 30 * MIN + 1
    post_obs("A", [(0, 1.0), (1000, 2.0), (low_at, 0.5), (low_at + 1000, 2.0)])
    # 高点与低点间隔 >30 分钟 -> 分属不同片段，无法成段
    assert segs() == []


# ---------------------------------------------------------------- 范围与持久化

def test_query_filters_by_later_turning_point_inclusive():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 1.0), (3000, 2.0), (4000, 0.5)])
    assert [s["end_utc_ms"] for s in segs()] == [2000, 3000]
    assert [s["end_utc_ms"] for s in segs(0, 2000)] == [2000]  # 上界含端点
    assert [s["end_utc_ms"] for s in segs(2001, 10**9)] == [3000]
    assert query(2001, 2999)["count"] == 0


def test_segments_are_persisted_and_served_from_table():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    assert query(0, 10**9)["count"] == 1
    # 直接改表后查询如实反映表内值，说明查询直接读持久化潮段
    conn = db.get_conn()
    with db.lock():
        conn.execute("UPDATE tide_segments SET end_tide_level = 42.0")
    assert query(0, 10**9)["items"][0]["end_tide_level"] == 42.0


def test_old_volume_is_backfilled_on_first_use(tmp_path, monkeypatch):
    path = tmp_path / "old-buoy.db"
    # 构造“旧卷”：有原文/换算结果，但没有潮段表，也没有非有限标记列
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE calibration_points (
            boot_segment TEXT NOT NULL, counter INTEGER NOT NULL,
            utc_ms INTEGER NOT NULL, PRIMARY KEY (boot_segment, counter));
        CREATE TABLE observations (
            obs_id TEXT PRIMARY KEY, boot_segment TEXT NOT NULL,
            counter INTEGER NOT NULL, tide_level REAL NOT NULL,
            utc_ms INTEGER, status TEXT NOT NULL, reason TEXT,
            cal_lo_counter INTEGER, cal_lo_utc_ms INTEGER,
            cal_hi_counter INTEGER, cal_hi_utc_ms INTEGER);
        INSERT INTO calibration_points VALUES ('A', 0, 0), ('A', 10000, 10000);
        INSERT INTO observations
            (obs_id, boot_segment, counter, tide_level, utc_ms, status)
        VALUES ('old0', 'A', 0, 1.0, 0, 'ok'),
               ('old1', 'A', 1000, 2.0, 1000, 'ok'),
               ('old2', 'A', 2000, 0.5, 2000, 'ok'),
               ('old3', 'A', 3000, 1.5, 3000, 'ok');
    """)
    old.commit()
    old.close()

    monkeypatch.setenv("BUOY_DB", str(path))
    db.reset_for_tests()
    try:
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT start_obs_id, end_obs_id FROM tide_segments"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("old1", "old2")]
        # 旧卷缺的标记列已补齐，原文可读
        detail = client.get("/observations/old1").json()
        assert detail["tide_level"] == 2.0 and detail["status"] == "ok"
        assert query(0, 10**9)["count"] == 1
    finally:
        db.reset_for_tests()
        monkeypatch.undo()
        os.environ["BUOY_DB"] = ":memory:"


# ---------------------------------------------------------------- 增量重算

def test_overwrite_reports_removed_and_added_segments():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 2.0), (4000, 1.0)])
    assert endpoints({"segments_added": segs()}) == [
        ("A-o1", "A-o2"), ("A-o2", "A-o3"),
    ]

    # 把中间低点的潮位抬高 -> 旧两段端点潮位变化，旧段撤下、无新段
    r = client.post("/observations", json={"observations": [
        {"obs_id": "A-o2", "boot_segment": "A", "counter": 2000, "tide_level": 2.5},
    ]})
    body = r.json()
    assert {s["start_obs_id"] for s in body["segments_removed"]} == {"A-o1", "A-o2"}
    assert body["segments_removed_count"] == 2
    assert body["segments_added"] == []
    assert segs() == []


def test_calibration_move_withdraws_and_adds_segment():
    calibrate("A", [(0, 0), (10000, 10000)])
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 3.0), (4000, 1.0)])
    assert segs()

    # 重新校时使 t=1000 的高点移动到 t=1200：端点变了 -> 旧段撤下、新段列入
    body = client.post("/calibrations", json={"calibrations": [
        {"boot_segment": "A", "counter": 1000, "utc_ms": 1200},
    ]}).json()
    assert body["segments_removed_count"] >= 1
    assert body["segments_added_count"] >= 1
    moved = [s for s in segs() if s["start_obs_id"] == "A-o1"]
    assert moved and moved[0]["start_utc_ms"] == 1200
    assert all(s["start_utc_ms"] != 1000 for s in segs())


def test_only_affected_boot_segment_is_recomputed():
    anchor("A")
    calibrate("B", [(0, 100_000), (10_000_000, 10_100_000)])
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    post_obs("B", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    a_seg = [s for s in segs() if s["boot_segment"] == "A"]
    b_seg = [s for s in segs() if s["boot_segment"] == "B"]
    assert a_seg and b_seg

    # 改写 B 段读数使 B 段潮段变化：只重算 B，A 段不应出现在回执里
    r = client.post("/observations", json={"observations": [
        {"obs_id": "B-o2", "boot_segment": "B", "counter": 2000, "tide_level": 2.5},
    ]})
    body = r.json()
    assert body["segments_added"] or body["segments_removed"]
    assert all(s["boot_segment"] == "B" for s in body["segments_added"])
    assert all(s["boot_segment"] == "B" for s in body["segments_removed"])
    assert [s for s in segs() if s["boot_segment"] == "A"] == a_seg


def test_moving_observation_to_another_boot_segment_refreshes_both():
    anchor("A")
    calibrate("B", [(0, 0), (10_000_000, 10_000_000)])
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    assert len([s for s in segs() if s["boot_segment"] == "A"]) == 1

    # 把 A 段的高点改投到 B 段：A 段旧段撤下，B 段按新读数重算
    r = client.post("/observations", json={"observations": [
        {"obs_id": "A-o1", "boot_segment": "B", "counter": 1000, "tide_level": 2.0},
    ]})
    body = r.json()
    assert any(s["boot_segment"] == "A" for s in body["segments_removed"])
    assert [s for s in segs() if s["boot_segment"] == "A"] == []
    assert client.get("/observations/A-o1").json()["boot_segment"] == "B"


def test_rejected_calibration_batch_keeps_segments():
    anchor("A")
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    before = segs()
    assert before

    r = client.post("/calibrations", json={"calibrations": [
        {"boot_segment": "A", "counter": 15_000_000, "utc_ms": 100},  # UTC 倒退
    ]})
    assert r.status_code == 409
    assert segs() == before
    assert client.get("/observations/A-o0").json()["utc_ms"] == 0


def test_identical_resubmission_lists_no_segment_change():
    anchor()
    items = [
        {"obs_id": "A-o0", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "A-o1", "boot_segment": "A", "counter": 1000, "tide_level": 2.0},
        {"obs_id": "A-o2", "boot_segment": "A", "counter": 2000, "tide_level": 0.5},
        {"obs_id": "A-o3", "boot_segment": "A", "counter": 3000, "tide_level": 1.5},
    ]
    r1 = client.post("/observations", json={"observations": items})
    assert r1.json()["segments_added_count"] == 1
    r2 = client.post("/observations", json={"observations": items})
    assert r2.json()["segments_added"] == []
    assert r2.json()["segments_removed"] == []


def test_out_of_range_readings_confirm_turning_points():
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    # 查询范围只覆盖后一拐点 2000，前后读数都在范围外
    items = query(1500, 2500)["items"]
    assert len(items) == 1
    seg = items[0]
    assert (seg["start_utc_ms"], seg["end_utc_ms"]) == (1000, 2000)
    # 范围外的 t=0 与 t=3000 分别参与确认 t=1000 高点与 t=2000 低点
    assert seg["start_tide_level"] == 2.0 and seg["end_tide_level"] == 0.5


def test_boot_segments_never_connect_across_boundaries():
    anchor("A")
    # B 段起点晚于 A 段终点，两段读数不可能连成一段
    calibrate("B", [(0, 50_000), (100_000, 150_000)])
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    post_obs("B", [(0, 0.2), (1000, 3.0), (2000, 0.4), (3000, 2.0)])
    items = segs(0, 10_000_000)
    by_seg = {s["boot_segment"] for s in items}
    assert by_seg == {"A", "B"}
    # 每段两端观测的 ID 前缀都来自同一开机段
    for s in items:
        prefix = s["boot_segment"] + "-"
        assert s["start_obs_id"].startswith(prefix)
        assert s["end_obs_id"].startswith(prefix)


def test_failed_write_rolls_back_raw_calibration_and_segments(monkeypatch):
    anchor()
    post_obs("A", [(0, 1.0), (1000, 2.0), (2000, 0.5), (3000, 1.5)])
    before_segments = segs()
    before_cal = client.get("/observations/A-o1").json()

    import app.core as core_mod

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(core_mod, "refresh_tide_segments", boom)
    failing = TestClient(app, raise_server_exceptions=False)
    r = failing.post("/observations", json={"observations": [
        {"obs_id": "A-new", "boot_segment": "A", "counter": 500, "tide_level": 9.0},
    ]})
    assert r.status_code == 500

    monkeypatch.undo()
    # 原文未落库，校时与潮段保持提交前状态
    assert client.get("/observations/A-new").status_code == 404
    assert client.get("/observations/A-o1").json() == before_cal
    assert segs() == before_segments


def test_help_text_and_openapi_expose_segment_route():
    assert "/tide-segments" in client.get("/openapi.json").json()["paths"]
    assert client.get("/docs").status_code == 200
    assert client.get(
        "/tide-segments", params={"start_ms": 5, "end_ms": 1}
    ).status_code == 422
