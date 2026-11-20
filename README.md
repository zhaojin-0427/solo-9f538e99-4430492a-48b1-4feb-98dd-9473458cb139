# 单浮标潮位校时服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。非有限潮位（`NaN`/`±inf`）可提交
  （JSON 中以 `"nan"`/`"inf"`/`"-inf"` 字符串表示），原文照常保存，但该时刻
  不参与潮段推导。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；更正校时点或覆盖观测解除冲突后自动恢复。
  同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，旧数据照常供数。

### 潮段（tide segment）

潮段由**单个开机段**内相邻的一对实测高/低拐点连成，各开机段独立计算
（不同段之间绝不连线）。规则：

- 时间线取各段的 `status=ok` 观测，按 UTC 毫秒升序。
- **断点**：以下情况处时间线断开，断点两侧不连线——
  同一毫秒出现不同潮位、该毫秒存在冲突、潮位非有限（`±inf`/`NaN`），
  以及相邻读数间隔 **超过 30 分钟**（恰好 30 分钟仍相连）。
- **同刻同值**：同一毫秒的多条等值读数合并为一点，取观测 ID 最小的一条。
- **连续等值**：相邻的等值读数视作一个点，保留首条的时刻与观测 ID。
- **拐点**：某点潮位比前后相邻点都高为高点、都低为低点；片段首尾点
  不判为拐点。片段内相邻两个拐点构成一段潮段。
- 潮差为两端潮位之差的绝对值；`direction` 为 `rising`（涨潮，后端点更高）
  或 `falling`（落潮，后端点更低）。
- 查询按**后一拐点**（较晚拐点）的 UTC 毫秒范围筛选，区间含端点；
  范围外的读数仍参与确认拐点（片段端点效应按全量读数计算）。

潮段在提交时随观测/校时一并重算并**持久保存**在 `tide_segments` 表：
每次提交只重算受影响的开机段（含冲突变化波及的其他段），查询直接读表。
旧 SQLite 卷首次启用时会自动补齐一次潮段。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新 + 潮段同步）都在**单个 SQLite
事务**中完成，并由进程级锁串行化读写：原文、校时依据、分析供数、潮段同次
生效；写入失败整批回滚，查询和（崩溃后的）重启都不会看到半次重算的中间态。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34}
  ]
}
```

### `POST /calibrations` — 分批提交校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

两个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测 ID**
（含新观测，以及被卷入/解除冲突的其他观测），并列出**本次新增和撤下的潮段**：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}],
  "segments_added_count": 1,
  "segments_added": [{
    "boot_segment": "boot-1", "direction": "rising",
    "start_utc_ms": 1700000000000, "start_tide_level": 0.8,
    "start_obs_id": "obs-low", "end_utc_ms": 1700003600000,
    "end_tide_level": 2.4, "end_obs_id": "obs-high", "tidal_range": 1.6
  }],
  "segments_removed_count": 0,
  "segments_removed": []
}
```

潮段的任一端点时刻或潮位变化时，旧潮段会列入 `segments_removed`、
对应新潮段列入 `segments_added`。

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。

### `GET /tide-segments?start_ms=...&end_ms=...` — 按后一拐点查潮段

返回**后一拐点**（较晚拐点）落在 `[start_ms, end_ms]` 内的潮段，区间含端点，
按后一拐点时刻升序。每条记录含开机段、涨落方向、两端时刻/潮位/观测 ID 和潮差
（字段同上方 `segments_added` 的元素）。各开机段单独成段。

```json
{
  "count": 1,
  "items": [{"boot_segment": "boot-1", "direction": "falling", "...": "..."}]
}
```

### `GET /observations/{obs_id}` — 按观测 ID 查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py   # FastAPI 路由与请求/响应模型
  core.py   # 校时换算、批量写入、重算、冲突检测、潮段推导与同步
  db.py     # SQLite 连接、模式、迁移、事务与锁
tests/
  test_api.py            # 校时/冲突/整批拒绝/查询
  test_tide_segments.py  # 潮段推导、持久化、增量重算
Dockerfile
docker-compose.yml
```
