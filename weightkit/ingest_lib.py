"""样本接收与基准发布的核心逻辑（HTTP 服务与测试共用）。

只负责"收货"：校验、入库。绝不触碰权重计算 —— 权重由独立的计算服务产出。
"""

import csv
import io

from .db import utcnow


class IngestError(ValueError):
    """带行号的校验错误，直接返回给上传者。"""


def _read_csv(csv_text, required_cols):
    if not csv_text or not csv_text.strip():
        raise IngestError("CSV 内容为空")
    reader = csv.DictReader(io.StringIO(csv_text))
    missing = [c for c in required_cols if c not in (reader.fieldnames or [])]
    if missing:
        raise IngestError(f"缺少必需列: {', '.join(missing)}；需要列: {', '.join(required_cols)}")
    return list(reader)


def import_benchmark_csv(conn, name, note, csv_text):
    """发布一个基准版本。返回 (version_id, 单元数, 总人口)。"""
    rows = _read_csv(csv_text, ["region", "sex", "age_group", "population"])
    if not name or not name.strip():
        raise IngestError("基准版本名称不能为空")
    cells = []
    for i, row in enumerate(rows, start=2):  # 第 1 行是表头
        region = (row.get("region") or "").strip()
        sex = (row.get("sex") or "").strip()
        age = (row.get("age_group") or "").strip()
        if not region or not sex or not age:
            raise IngestError(f"第 {i} 行: region/sex/age_group 不能为空")
        try:
            pop = float((row.get("population") or "").strip())
        except ValueError:
            raise IngestError(f"第 {i} 行: population 不是数字: {row.get('population')!r}")
        if pop < 0:
            raise IngestError(f"第 {i} 行: population 不能为负")
        cells.append((region, sex, age, pop))
    if not cells:
        raise IngestError("没有任何数据行")

    with conn:  # 版本行 + 全部单元一个事务，避免半个版本
        cur = conn.execute(
            "INSERT INTO benchmark_versions(name, note, created_at) VALUES (?,?,?)",
            (name.strip(), note or "", utcnow()),
        )
        version_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO benchmark_cells(version_id, region, sex, age_group, population)"
            " VALUES (?,?,?,?,?)",
            [(version_id, r, s, a, p) for r, s, a, p in cells],
        )
    return version_id, len(cells), sum(c[3] for c in cells)


def import_sample_csv(conn, source, note, csv_text):
    """接收一个补报批次。返回 (batch_id, 入库条数, 涉及的层列表)。"""
    rows = _read_csv(
        csv_text,
        ["respondent_id", "region", "sex", "age_group", "satisfaction", "used_service"],
    )
    if not source or not source.strip():
        raise IngestError("批次来源 source 不能为空")
    records = []
    seen = set()
    for i, row in enumerate(rows, start=2):
        rid = (row.get("respondent_id") or "").strip()
        region = (row.get("region") or "").strip()
        sex = (row.get("sex") or "").strip()
        age = (row.get("age_group") or "").strip()
        if not rid or not region or not sex or not age:
            raise IngestError(f"第 {i} 行: respondent_id/region/sex/age_group 不能为空")
        if rid in seen:
            raise IngestError(f"第 {i} 行: 批次内 respondent_id 重复: {rid}")
        seen.add(rid)
        sat_raw = (row.get("satisfaction") or "").strip()
        if sat_raw:
            try:
                sat = float(sat_raw)
            except ValueError:
                raise IngestError(f"第 {i} 行: satisfaction 不是数字: {sat_raw!r}")
            if not (1 <= sat <= 5):
                raise IngestError(f"第 {i} 行: satisfaction 超出 1..5 范围: {sat}")
        else:
            sat = None
        used_raw = (row.get("used_service") or "").strip()
        if used_raw:
            if used_raw not in ("0", "1"):
                raise IngestError(f"第 {i} 行: used_service 只能是 0 或 1: {used_raw!r}")
            used = int(used_raw)
        else:
            used = None
        records.append((rid, region, sex, age, sat, used))
    if not records:
        raise IngestError("没有任何数据行")

    now = utcnow()
    with conn:
        cur = conn.execute(
            "INSERT INTO sample_batches(source, note, received_at) VALUES (?,?,?)",
            (source.strip(), note or "", now),
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO sample_records"
            "(batch_id, respondent_id, region, sex, age_group, satisfaction, used_service, received_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(batch_id, *rec, now) for rec in records],
        )
    strata = sorted({f"{r[1]}|{r[2]}|{r[3]}" for r in records})
    return batch_id, len(records), strata


def list_benchmarks(conn):
    rows = conn.execute(
        "SELECT v.id, v.name, v.note, v.created_at, COUNT(c.region) AS cells,"
        "       COALESCE(SUM(c.population), 0) AS total_population"
        " FROM benchmark_versions v"
        " LEFT JOIN benchmark_cells c ON c.version_id = v.id"
        " GROUP BY v.id ORDER BY v.id"
    ).fetchall()
    return [dict(r) for r in rows]


def list_batches(conn):
    rows = conn.execute(
        "SELECT b.id, b.source, b.note, b.received_at, COUNT(r.id) AS n_records"
        " FROM sample_batches b"
        " LEFT JOIN sample_records r ON r.batch_id = b.id"
        " GROUP BY b.id ORDER BY b.id"
    ).fetchall()
    return [dict(r) for r in rows]
