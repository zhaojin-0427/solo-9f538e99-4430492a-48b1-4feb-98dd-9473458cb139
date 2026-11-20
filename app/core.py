"""校时换算、批量写入、重算与冲突检测的核心逻辑。

状态机（observations.status）：
  ok                   已校时且无冲突，供分析
  pending_calibration  计数落在本段校时区间外，保留原文、待校时
  conflict             不同开机段落在同一 UTC 毫秒，冲突观测均不供分析

潮段（tide_segments）：
  按开机段独立地从可用观测中识别高/低拐点，连接同一未断开片段内
  相邻的一对高/低拐点。提交后只重算受影响的开机段，查询直接读表。

所有函数都假定运行在 db.transaction 包裹的同一事务里，
因此“原文 + 校时依据 + 分析供数 + 潮段”同次生效。
"""

from __future__ import annotations

import bisect
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"

RISING = "rising"
FALLING = "falling"

# 相邻读数间隔超过 30 分钟即断开
MAX_READING_GAP_MS = 30 * 60 * 1000

# 旧卷首次启用时写入该标记，避免每次启动都重算
SEGMENTS_INITIALIZED_KEY = "tide_segments_initialized"


@dataclass
class BatchOutcome:
    """一次提交的回执：原变更观测 + 本次新增/撤下的潮段。"""

    changed_ids: list[str] = field(default_factory=list)
    added_segments: list[dict] = field(default_factory=list)
    removed_segments: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- 潮位编码

def encode_tide_level(value: float) -> tuple[float, int]:
    """SQLite 不能存 NaN；非有限潮位统一记 0.0 + 标记（1=nan,2=+inf,3=-inf）。"""
    if math.isnan(value):
        return 0.0, 1
    if math.isinf(value):
        return 0.0, 2 if value > 0 else 3
    return float(value), 0


def decode_tide_level(value: float, marker: int) -> float:
    if marker == 1:
        return float("nan")
    if marker == 2:
        return float("inf")
    if marker == 3:
        return float("-inf")
    return value



# ---------------------------------------------------------------- 校时换算

def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部校时点，按计数升序：[(counter, utc_ms), ...]"""
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    return [(r["counter"], r["utc_ms"]) for r in rows]


def compute_utc(
    points: list[tuple[int, int]], counter: int
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """相邻校时点间线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中某个校时点时直接取该点 UTC（上下界相同）。
    """
    if not points:
        return None
    counters = [p[0] for p in points]
    i = bisect.bisect_left(counters, counter)
    if i < len(points) and counters[i] == counter:
        p = points[i]
        return p[1], p, p
    if i == 0 or i == len(points):
        return None
    (c1, t1), (c2, t2) = points[i - 1], points[i]
    # 段内 UTC 单调不倒退已由写入方校验，故各项均非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, (c1, t1), (c2, t2)


def _pending_reason(segment: str, counter: int, points: list[tuple[int, int]]) -> str:
    if not points:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{points[0][0]}, {points[-1][0]}]，待校时"
    )


# ---------------------------------------------------------------- 重算与冲突

def recalc_segment(conn: sqlite3.Connection, segment: str) -> None:
    """按当前校时点重算某开机段全部观测的 utc_ms / 状态 / 校时依据。"""
    points = load_points(conn, segment)
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        result = compute_utc(points, row["counter"])
        if result is None:
            conn.execute(
                "UPDATE observations SET utc_ms = NULL, status = ?, reason = ?, "
                "cal_lo_counter = NULL, cal_lo_utc_ms = NULL, "
                "cal_hi_counter = NULL, cal_hi_utc_ms = NULL "
                "WHERE obs_id = ?",
                (PENDING, _pending_reason(segment, row["counter"], points), row["obs_id"]),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "UPDATE observations SET utc_ms = ?, status = ?, reason = NULL, "
                "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
                "cal_hi_counter = ?, cal_hi_utc_ms = ? "
                "WHERE obs_id = ?",
                (utc_ms, OK, c_lo, t_lo, c_hi, t_hi, row["obs_id"]),
            )


def conflict_ms_set(conn: sqlite3.Connection) -> set[int]:
    """被 >=2 个不同开机段的观测占用的 UTC 毫秒集合。"""
    rows = conn.execute(
        "SELECT utc_ms FROM observations WHERE utc_ms IS NOT NULL "
        "GROUP BY utc_ms HAVING COUNT(DISTINCT boot_segment) > 1"
    ).fetchall()
    return {r["utc_ms"] for r in rows}


def resolve_conflicts(conn: sqlite3.Connection) -> None:
    """全局刷新冲突状态：新冲突标记 conflict，已解除的恢复 ok。"""
    ms_set = conflict_ms_set(conn)
    conflicted_ids: set[str] = set()
    if ms_set:
        ph = ",".join("?" for _ in ms_set)
        rows = conn.execute(
            f"SELECT obs_id, utc_ms, boot_segment FROM observations "
            f"WHERE utc_ms IN ({ph})",
            tuple(sorted(ms_set)),
        ).fetchall()
        by_ms: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            by_ms.setdefault(r["utc_ms"], []).append(r)
        for ms, group in by_ms.items():
            segments = {g["boot_segment"] for g in group}
            for g in group:
                others = "、".join(sorted(segments - {g["boot_segment"]}))
                reason = f"UTC 毫秒 {ms} 与开机段 {others} 的观测冲突，不供分析"
                conn.execute(
                    "UPDATE observations SET status = ?, reason = ? WHERE obs_id = ?",
                    (CONFLICT, reason, g["obs_id"]),
                )
                conflicted_ids.add(g["obs_id"])
    # 解除冲突后恢复
    if conflicted_ids:
        ph = ",".join("?" for _ in conflicted_ids)
        conn.execute(
            f"UPDATE observations SET status = ?, reason = NULL "
            f"WHERE status = ? AND obs_id NOT IN ({ph})",
            (OK, CONFLICT, *sorted(conflicted_ids)),
        )
    else:
        conn.execute(
            "UPDATE observations SET status = ?, reason = NULL WHERE status = ?",
            (OK, CONFLICT),
        )


# ---------------------------------------------------------------- 潮段计算

def _conflict_instants(conn: sqlite3.Connection) -> set[int]:
    """全部冲突时刻：这些毫秒不供分析，作为时间线的断点。"""
    rows = conn.execute(
        "SELECT DISTINCT utc_ms FROM observations "
        "WHERE status = ? AND utc_ms IS NOT NULL",
        (CONFLICT,),
    ).fetchall()
    return {r["utc_ms"] for r in rows}


def _collapse_equal(points: list[tuple[int, float, str]]) -> list[tuple[int, float, str]]:
    """连续等值读数视作一个点，保留首条的时刻与观测 ID。"""
    collapsed: list[tuple[int, float, str]] = []
    for p in points:
        if collapsed and collapsed[-1][1] == p[1]:
            continue
        collapsed.append(p)
    return collapsed


def compute_tide_segments(
    conn: sqlite3.Connection, boot_segment: str, conflict_instants: set[int]
) -> list[dict]:
    """按单个开机段从可用观测推导潮段（不落库）。

    规则：
      - 同刻同值取 ID 最小的读数；同刻异值、冲突时刻、非有限潮位时刻断开。
      - 相邻读数间隔超过 30 分钟断开。
      - 连续等值读数合并为一个点（保留首条）。
      - 片段内前后相邻点都更低/更高者为高/低拐点，连接相邻拐点成潮段。
    """
    rows = conn.execute(
        "SELECT obs_id, utc_ms, tide_level, tide_level_nonfinite "
        "FROM observations WHERE boot_segment = ? AND status = ? "
        "AND utc_ms IS NOT NULL ORDER BY utc_ms, obs_id",
        (boot_segment, OK),
    ).fetchall()

    by_ms: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_ms.setdefault(r["utc_ms"], []).append(r)

    points: list[tuple[int, float, str]] = []
    # 任何冲突时刻都视为时间线断点（该时刻不可信）
    break_instants: set[int] = set(conflict_instants)
    for ms, group in by_ms.items():
        if ms in conflict_instants:
            continue
        values = [
            decode_tide_level(g["tide_level"], g["tide_level_nonfinite"])
            for g in group
        ]
        if any(not math.isfinite(v) for v in values):
            break_instants.add(ms)
            continue
        if len(set(values)) > 1:  # 同刻异值：该时刻不可信，断开
            break_instants.add(ms)
            continue
        first = min(group, key=lambda r: r["obs_id"])  # 同刻同值取最小 ID
        points.append((ms, values[0], first["obs_id"]))

    points.sort(key=lambda p: (p[0], p[2]))
    breaks = sorted(break_instants)

    # 片段：间隔 >30 分钟，或两点之间夹有断点时刻
    fragments: list[list[tuple[int, float, str]]] = []
    current: list[tuple[int, float, str]] = []
    for p in points:
        if current:
            prev = current[-1]
            too_far = p[0] - prev[0] > MAX_READING_GAP_MS
            bi = bisect.bisect_right(breaks, prev[0])
            has_break = bi < len(breaks) and breaks[bi] < p[0]
            if too_far or has_break:
                fragments.append(current)
                current = []
        current.append(p)
    if current:
        fragments.append(current)

    segments: list[dict] = []
    for fragment in fragments:
        points_in = _collapse_equal(fragment)
        turns: list[tuple[int, float, str]] = []
        for i in range(1, len(points_in) - 1):
            level = points_in[i][1]
            if level > points_in[i - 1][1] and level > points_in[i + 1][1]:
                turns.append(points_in[i])  # 高点
            elif level < points_in[i - 1][1] and level < points_in[i + 1][1]:
                turns.append(points_in[i])  # 低点
        for start, end in zip(turns, turns[1:]):
            if end[1] == start[1]:
                continue
            segments.append({
                "boot_segment": boot_segment,
                "direction": RISING if end[1] > start[1] else FALLING,
                "start_utc_ms": start[0],
                "start_tide_level": start[1],
                "start_obs_id": start[2],
                "end_utc_ms": end[0],
                "end_tide_level": end[1],
                "end_obs_id": end[2],
                "tidal_range": abs(end[1] - start[1]),
            })
    segments.sort(key=_segment_order)
    return segments


SEGMENT_FIELDS = (
    "boot_segment", "direction", "start_utc_ms", "start_tide_level",
    "start_obs_id", "end_utc_ms", "end_tide_level", "end_obs_id", "tidal_range",
)


def _segment_order(seg: dict) -> tuple:
    return (seg["end_utc_ms"], seg["start_utc_ms"], seg["start_obs_id"],
            seg["end_obs_id"])


def _segment_key(seg: dict) -> tuple[str, str]:
    return (seg["start_obs_id"], seg["end_obs_id"])


def load_tide_segments(conn: sqlite3.Connection, boot_segment: str) -> dict[tuple, dict]:
    rows = conn.execute(
        f"SELECT {', '.join(SEGMENT_FIELDS)} FROM tide_segments "
        f"WHERE boot_segment = ?",
        (boot_segment,),
    ).fetchall()
    return {_segment_key({f: r[f] for f in SEGMENT_FIELDS}):
            {f: r[f] for f in SEGMENT_FIELDS} for r in rows}


def sync_tide_segments(
    conn: sqlite3.Connection, boot_segment: str, conflict_instants: set[int]
) -> tuple[list[dict], list[dict]]:
    """把一个开机段的潮段重算结果同步到表，返回 (新增, 撤下)。

    “撤下”包括整体消失以及端点时刻/潮位等任一字段变化（旧段撤下、新段列入）。
    """
    desired = {_segment_key(s): s for s in
               compute_tide_segments(conn, boot_segment, conflict_instants)}
    existing = load_tide_segments(conn, boot_segment)

    removed: list[dict] = []
    added: list[dict] = []
    for key, old in existing.items():
        new = desired.get(key)
        if new is None or any(new[f] != old[f] for f in SEGMENT_FIELDS):
            removed.append(old)
    for key, new in desired.items():
        old = existing.get(key)
        if old is None or any(new[f] != old[f] for f in SEGMENT_FIELDS):
            added.append(new)

    for seg in removed:
        conn.execute(
            "DELETE FROM tide_segments WHERE boot_segment = ? "
            "AND start_obs_id = ? AND end_obs_id = ?",
            (boot_segment, seg["start_obs_id"], seg["end_obs_id"]),
        )
    for seg in added:
        conn.execute(
            f"INSERT OR REPLACE INTO tide_segments ({', '.join(SEGMENT_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in SEGMENT_FIELDS)})",
            tuple(seg[f] for f in SEGMENT_FIELDS),
        )

    removed.sort(key=_segment_order)
    added.sort(key=_segment_order)
    return added, removed


def refresh_tide_segments(
    conn: sqlite3.Connection, boot_segments: Iterable[str]
) -> tuple[list[dict], list[dict]]:
    """只重算给定开机段的潮段（受本次提交波及的段）。"""
    conflict_instants = _conflict_instants(conn)
    added: list[dict] = []
    removed: list[dict] = []
    for seg in sorted(set(boot_segments)):
        a, r = sync_tide_segments(conn, seg, conflict_instants)
        added.extend(a)
        removed.extend(r)
    added.sort(key=lambda s: (_segment_order(s), s["boot_segment"]))
    removed.sort(key=lambda s: (_segment_order(s), s["boot_segment"]))
    return added, removed


def ensure_tide_segments(conn: sqlite3.Connection) -> None:
    """旧 SQLite 卷首次启用时补齐潮段；已有标记则直接跳过。"""
    from . import db  # 延迟导入，避免循环

    with db.lock():
        with db.transaction(conn):
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (SEGMENTS_INITIALIZED_KEY,)
            ).fetchone()
            if row is not None:
                return
            segments = [
                r["boot_segment"]
                for r in conn.execute(
                    "SELECT DISTINCT boot_segment FROM observations"
                )
            ]
            refresh_tide_segments(conn, segments)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (SEGMENTS_INITIALIZED_KEY, "1"),
            )


# ---------------------------------------------------------------- 变更快照

def _snapshot(conn: sqlite3.Connection, where: str, params: Iterable) -> dict[str, tuple]:
    """obs_id -> (utc_ms, status, boot_segment)，用于事务内前后对比。"""
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status, boot_segment FROM observations WHERE {where}",
        tuple(params),
    ).fetchall()
    return {
        r["obs_id"]: (r["utc_ms"], r["status"], r["boot_segment"]) for r in rows
    }


def _changed_ids(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """时刻或状态发生变化（含新出现）的观测 ID。"""
    return sorted(
        oid
        for oid, rec in after.items()
        if oid not in before or before[oid][:2] != rec[:2]
    )


def fetch_states(conn: sqlite3.Connection, obs_ids: list[str]) -> list[dict]:
    """读取给定观测的当前 (obs_id, utc_ms, status)，供提交响应使用。"""
    if not obs_ids:
        return []
    ph = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations "
        f"WHERE obs_id IN ({ph}) ORDER BY obs_id",
        obs_ids,
    ).fetchall()
    return [{"obs_id": r["obs_id"], "utc_ms": r["utc_ms"], "status": r["status"]} for r in rows]


def _affected_segments(
    before: dict[str, tuple], after: dict[str, tuple], changed: list[str]
) -> set[str]:
    """本次变更波及的开机段（含观测原属段与现属段）。"""
    segs: set[str] = set()
    for oid in changed:
        if oid in before:
            segs.add(before[oid][2])
        if oid in after:
            segs.add(after[oid][2])
    return segs


# ---------------------------------------------------------------- 批量写入

def upsert_observations(conn: sqlite3.Connection, items: list[dict]) -> BatchOutcome:
    """分批写入观测（同 ID 覆盖原文并重算），返回回执。

    新观测可能与其他开机段落在同一 UTC 毫秒而触发冲突，
    因此写入后做全局冲突刷新，并把受波及的观测与开机段的潮段一并重算。
    """
    if not items:
        return BatchOutcome()
    segments = {it["boot_segment"] for it in items}
    points_by_seg = {s: load_points(conn, s) for s in segments}
    ids = [it["obs_id"] for it in items]
    id_ph = ",".join("?" for _ in ids)

    # 覆盖前后的开关段（改段时两侧的时间线都要重算）
    old_segments_of_ids = {
        r["obs_id"]: r["boot_segment"]
        for r in conn.execute(
            f"SELECT obs_id, boot_segment FROM observations WHERE obs_id IN ({id_ph})",
            ids,
        )
    }

    # 被覆盖观测的旧 UTC 毫秒（其上的冲突可能因此解除）
    old_ms = {
        r["utc_ms"]
        for r in conn.execute(
            f"SELECT utc_ms FROM observations "
            f"WHERE obs_id IN ({id_ph}) AND utc_ms IS NOT NULL",
            ids,
        )
    }

    # 同批同 ID 后者覆盖前者
    computed: dict[str, tuple[dict, tuple | None]] = {}
    for it in items:
        computed[it["obs_id"]] = (it, compute_utc(points_by_seg[it["boot_segment"]], it["counter"]))

    new_ms = {res[0] for _, res in computed.values() if res is not None}
    affected_ms = old_ms | new_ms
    if affected_ms:
        ms_ph = ",".join("?" for _ in affected_ms)
        where = f"obs_id IN ({id_ph}) OR utc_ms IN ({ms_ph})"
        params: tuple = (*ids, *sorted(affected_ms))
    else:
        where = f"obs_id IN ({id_ph})"
        params = tuple(ids)
    before = _snapshot(conn, where, params)

    for obs_id, (it, result) in computed.items():
        stored_level, level_flag = encode_tide_level(it["tide_level"])
        if result is None:
            reason = _pending_reason(it["boot_segment"], it["counter"],
                                     points_by_seg[it["boot_segment"]])
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "tide_level_nonfinite, utc_ms, status, reason) "
                "VALUES (?,?,?,?,?,NULL,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, "
                "tide_level_nonfinite=excluded.tide_level_nonfinite, utc_ms=NULL, "
                "status=excluded.status, reason=excluded.reason, "
                "cal_lo_counter=NULL, cal_lo_utc_ms=NULL, "
                "cal_hi_counter=NULL, cal_hi_utc_ms=NULL",
                (it["obs_id"], it["boot_segment"], it["counter"], stored_level,
                 level_flag, PENDING, reason),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "tide_level_nonfinite, utc_ms, status, reason, cal_lo_counter, "
                "cal_lo_utc_ms, cal_hi_counter, cal_hi_utc_ms) "
                "VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, "
                "tide_level_nonfinite=excluded.tide_level_nonfinite, "
                "utc_ms=excluded.utc_ms, "
                "status=excluded.status, reason=NULL, "
                "cal_lo_counter=excluded.cal_lo_counter, "
                "cal_lo_utc_ms=excluded.cal_lo_utc_ms, "
                "cal_hi_counter=excluded.cal_hi_counter, "
                "cal_hi_utc_ms=excluded.cal_hi_utc_ms",
                (it["obs_id"], it["boot_segment"], it["counter"], stored_level,
                 level_flag, utc_ms, OK, c_lo, t_lo, c_hi, t_hi),
            )

    resolve_conflicts(conn)
    after = _snapshot(conn, where, params)
    changed = _changed_ids(before, after)
    # 本次提交涉及的段 + 被覆盖观测的原段 + 因冲突翻转受影响的其他段；
    # 不重算无关的开机段。
    touched = set(segments) | set(old_segments_of_ids.values())
    touched |= _affected_segments(before, after, changed)
    added, removed = refresh_tide_segments(conn, touched)
    return BatchOutcome(changed, added, removed)


class MonotonicViolation(Exception):
    """校时点使段内 UTC 倒退；violations 携带明细。"""

    def __init__(self, violations: list[dict]):
        super().__init__("calibration points make UTC go backwards within a segment")
        self.violations = violations


def upsert_calibrations(conn: sqlite3.Connection, items: list[dict]) -> BatchOutcome:
    """分批写入校时点（同段同计数替换旧值），重算受影响观测与潮段。

    合并新旧校时点后，任一开机段内 UTC 随计数倒退则抛 MonotonicViolation，
    由调用方回滚整批，旧数据、潮段照常供数。
    """
    if not items:
        return BatchOutcome()
    segments = {it["boot_segment"] for it in items}
    for it in items:
        conn.execute(
            "INSERT INTO calibration_points (boot_segment, counter, utc_ms) "
            "VALUES (?,?,?) "
            "ON CONFLICT(boot_segment, counter) DO UPDATE SET utc_ms=excluded.utc_ms",
            (it["boot_segment"], it["counter"], it["utc_ms"]),
        )

    violations: list[dict] = []
    for seg in sorted(segments):
        points = load_points(conn, seg)
        for (c1, t1), (c2, t2) in zip(points, points[1:]):
            if t2 < t1:
                violations.append({
                    "boot_segment": seg,
                    "counter": c2, "utc_ms": t2,
                    "prev_counter": c1, "prev_utc_ms": t1,
                })
    if violations:
        raise MonotonicViolation(violations)

    seg_ph = ",".join("?" for _ in segments)
    seg_params = tuple(sorted(segments))

    ms_before = conflict_ms_set(conn)
    if ms_before:
        ms_ph = ",".join("?" for _ in ms_before)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_before))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    before = _snapshot(conn, where, params)

    for seg in segments:
        recalc_segment(conn, seg)
    resolve_conflicts(conn)

    # 冲突集合可能变化，前后两批毫秒上的观测都可能翻转状态
    ms_all = ms_before | conflict_ms_set(conn)
    if ms_all:
        ms_ph = ",".join("?" for _ in ms_all)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_all))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    after = _snapshot(conn, where, params)
    changed = _changed_ids(before, after)
    # 本校时段 + 任何因冲突/时刻变化被波及的其他开机段
    added, removed = refresh_tide_segments(
        conn, segments | _affected_segments(before, after, changed)
    )
    return BatchOutcome(changed, added, removed)
