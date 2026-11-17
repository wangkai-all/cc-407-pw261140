"""SQLite 存储层：连接、schema、通用辅助。

所有服务共享同一个数据库文件（WAL 模式，支持多读单写并发）。
权重结果按 run（一次计算）整体写入，发布后不可变 —— 这是
"总表与分组报表权重一致" 的基础：报表永远只读 current_snapshot 指向的 run。
"""

import datetime
import json
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "weightkit.db")
DB_PATH = os.environ.get("WEIGHTKIT_DB", DEFAULT_DB)

SCHEMA = """
-- 人口基准版本（换版即新增一行，旧版本保留可追溯）
CREATE TABLE IF NOT EXISTS benchmark_versions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);

-- 基准单元：某版本下每个层的总体人口数
CREATE TABLE IF NOT EXISTS benchmark_cells (
  version_id  INTEGER NOT NULL REFERENCES benchmark_versions(id),
  region      TEXT NOT NULL,
  sex         TEXT NOT NULL,
  age_group   TEXT NOT NULL,
  population  REAL NOT NULL CHECK (population >= 0),
  PRIMARY KEY (version_id, region, sex, age_group)
);

-- 样本批次：每一次补报是一个批次
CREATE TABLE IF NOT EXISTS sample_batches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL
);

-- 样本记录（原始答卷，一旦入库不修改；补报只新增批次）
CREATE TABLE IF NOT EXISTS sample_records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id      INTEGER NOT NULL REFERENCES sample_batches(id),
  respondent_id TEXT NOT NULL,
  region        TEXT NOT NULL,
  sex           TEXT NOT NULL,
  age_group     TEXT NOT NULL,
  satisfaction  REAL,          -- 满意度 1..5，可空
  used_service  INTEGER,       -- 0/1，可空
  received_at   TEXT NOT NULL,
  UNIQUE (batch_id, respondent_id)
);
CREATE INDEX IF NOT EXISTS idx_records_stratum ON sample_records(region, sex, age_group);

-- 权重计算运行：full=全量重算, affected=只重算受影响层
CREATE TABLE IF NOT EXISTS weight_runs (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  mode                 TEXT NOT NULL CHECK (mode IN ('full','affected')),
  benchmark_version_id INTEGER NOT NULL REFERENCES benchmark_versions(id),
  parent_run_id        INTEGER REFERENCES weight_runs(id),
  status               TEXT NOT NULL CHECK (status IN
                         ('running','interrupted','completed','failed')),
  config_json          TEXT NOT NULL,      -- min_cell_size / trim 分位 / chunk 参数
  affected_json        TEXT,               -- 受影响层与波及区县（affected 模式）
  plan_json            TEXT NOT NULL DEFAULT '[]',  -- 待处理估计单元（有序）
  checkpoint           INTEGER NOT NULL DEFAULT 0,  -- 已处理 plan 项数（断点）
  base_copied          INTEGER NOT NULL DEFAULT 0,  -- affected 模式：未受影响部分是否已复制
  interrupt_requested  INTEGER NOT NULL DEFAULT 0,
  heartbeat_at         TEXT,
  created_at           TEXT NOT NULL,
  finished_at          TEXT,
  error                TEXT,
  stats_json           TEXT                -- 汇总：覆盖人口、修剪边界、校准因子等
);

-- 记录级权重：每条样本的权重依据（溯源核心）
CREATE TABLE IF NOT EXISTS record_weights (
  run_id               INTEGER NOT NULL REFERENCES weight_runs(id),
  record_id            INTEGER NOT NULL REFERENCES sample_records(id),
  cell_key             TEXT NOT NULL,      -- 最终估计单元（可能经过合并）
  merge_level          INTEGER NOT NULL,   -- 0=原层 1=合并年龄 2=合并性别
  n_cell               INTEGER NOT NULL,   -- 估计单元内样本数
  n_pop                REAL NOT NULL,      -- 估计单元对应基准人口
  raw_weight           REAL NOT NULL,      -- n_pop / n_cell
  weight               REAL NOT NULL,      -- 修剪 + 校准后的最终权重
  trimmed              INTEGER NOT NULL DEFAULT 0,
  benchmark_version_id INTEGER NOT NULL,   -- 冗余存储，便于单表导出溯源
  computed_at          TEXT NOT NULL,
  PRIMARY KEY (run_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_rw_run ON record_weights(run_id);

-- 估计单元（层/合并层）级结果
CREATE TABLE IF NOT EXISTS cell_weights (
  run_id            INTEGER NOT NULL REFERENCES weight_runs(id),
  cell_key          TEXT NOT NULL,
  region            TEXT NOT NULL,
  sex               TEXT,                  -- NULL 表示该维度已合并
  age_groups        TEXT NOT NULL,         -- JSON 数组：包含的年龄段
  merge_level       INTEGER NOT NULL,
  n_cell            INTEGER NOT NULL,
  n_pop             REAL NOT NULL,
  raw_weight        REAL NOT NULL,
  member_strata     TEXT NOT NULL,         -- JSON 数组：合并进来的原始层
  PRIMARY KEY (run_id, cell_key)
);

-- 无法估计的层：必须明确说明，人口计入"未覆盖"
CREATE TABLE IF NOT EXISTS unestimable_strata (
  run_id     INTEGER NOT NULL REFERENCES weight_runs(id),
  region     TEXT NOT NULL,
  sex        TEXT NOT NULL,
  age_group  TEXT NOT NULL,
  population REAL NOT NULL,
  reason     TEXT NOT NULL,
  PRIMARY KEY (run_id, region, sex, age_group)
);

-- 当前发布快照（单行表，原子切换；报表服务只读这里指向的 run）
CREATE TABLE IF NOT EXISTS current_snapshot (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  run_id       INTEGER NOT NULL REFERENCES weight_runs(id),
  published_at TEXT NOT NULL
);
"""


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    p = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def loads(text, default=None):
    if text is None:
        return default
    return json.loads(text)
