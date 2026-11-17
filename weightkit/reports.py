"""报表与修订比较：全部从"某一次运行"的权重快照计算。

一致性保证：总表与所有分组报表都带同一个 snapshot_id（run_id），
且只读 current_snapshot 指向的已发布运行 —— 不存在"总表已更新、
分组报表还用旧权重"的窗口。
"""

from .db import loads
from .weighting import HONESTY_NOTE, current_snapshot, get_run

DIMS = ("region", "sex", "age_group")
DIM_LABELS = {"region": "区县", "sex": "性别", "age_group": "年龄段"}


def _resolve_run(conn, run_id):
    if run_id is not None:
        return int(run_id)
    snap = current_snapshot(conn)
    if not snap:
        return None
    return snap["run_id"]


def _run_meta(conn, run_id):
    run = get_run(conn, run_id)
    name = conn.execute(
        "SELECT name FROM benchmark_versions WHERE id = ?", (run["benchmark_version_id"],)
    ).fetchone()["name"]
    stats = loads(run["stats_json"]) or {}
    return {
        "snapshot_id": run_id,
        "mode": run["mode"],
        "benchmark_version": name,
        "benchmark_version_id": run["benchmark_version_id"],
        "status": run["status"],
        "finished_at": run["finished_at"],
        "covered_population": stats.get("covered_population"),
        "uncovered_population": stats.get("uncovered_population"),
        "n_records_weighted": stats.get("n_records_weighted"),
        "n_trimmed": stats.get("n_trimmed"),
        "incremental_since_full": stats.get("incremental_since_full"),
        "warnings": stats.get("warnings", []),
        "honesty_note": HONESTY_NOTE,
    }


def master_report(conn, run_id=None):
    run_id = _resolve_run(conn, run_id)
    if run_id is None:
        return {"snapshot_id": None, "honesty_note": HONESTY_NOTE, "error": "尚未发布任何快照"}
    agg = conn.execute(
        "SELECT COUNT(*) AS n_records,"
        " COALESCE(SUM(w.weight), 0) AS sum_weight,"
        " SUM(w.weight * r.satisfaction) AS w_sat_num,"
        " SUM(CASE WHEN r.satisfaction IS NOT NULL THEN w.weight ELSE 0 END) AS w_sat_den,"
        " SUM(w.weight * r.used_service) AS w_used_num,"
        " SUM(CASE WHEN r.used_service IS NOT NULL THEN w.weight ELSE 0 END) AS w_used_den"
        " FROM record_weights w JOIN sample_records r ON r.id = w.record_id"
        " WHERE w.run_id = ?",
        (run_id,),
    ).fetchone()
    meta = _run_meta(conn, run_id)
    meta.update({
        "n_records": agg["n_records"],
        "sum_weight": agg["sum_weight"],
        "weighted_mean_satisfaction": (
            agg["w_sat_num"] / agg["w_sat_den"] if agg["w_sat_den"] else None
        ),
        "weighted_used_service_ratio": (
            agg["w_used_num"] / agg["w_used_den"] if agg["w_used_den"] else None
        ),
    })
    return meta


def group_report(conn, dim, run_id=None):
    if dim not in DIMS:
        raise ValueError(f"未知分组维度: {dim}，可选: {', '.join(DIMS)}")
    run_id = _resolve_run(conn, run_id)
    if run_id is None:
        return {"snapshot_id": None, "dimension": dim, "groups": [], "honesty_note": HONESTY_NOTE}
    rows = conn.execute(
        f"SELECT r.{dim} AS grp, COUNT(*) AS n_records,"
        f" COALESCE(SUM(w.weight), 0) AS sum_weight,"
        f" SUM(w.weight * r.satisfaction) AS sat_num,"
        f" SUM(CASE WHEN r.satisfaction IS NOT NULL THEN w.weight ELSE 0 END) AS sat_den,"
        f" SUM(w.weight * r.used_service) AS used_num,"
        f" SUM(CASE WHEN r.used_service IS NOT NULL THEN w.weight ELSE 0 END) AS used_den"
        f" FROM record_weights w JOIN sample_records r ON r.id = w.record_id"
        f" WHERE w.run_id = ? GROUP BY r.{dim} ORDER BY r.{dim}",
        (run_id,),
    ).fetchall()
    groups = [
        {
            "group": row["grp"],
            "n_records": row["n_records"],
            "sum_weight": row["sum_weight"],
            "weighted_mean_satisfaction": row["sat_num"] / row["sat_den"] if row["sat_den"] else None,
            "weighted_used_service_ratio": row["used_num"] / row["used_den"] if row["used_den"] else None,
        }
        for row in rows
    ]
    return {
        "snapshot_id": run_id,
        "dimension": dim,
        "dimension_label": DIM_LABELS[dim],
        "groups": groups,
        "honesty_note": HONESTY_NOTE,
    }


def unestimable_report(conn, run_id=None):
    run_id = _resolve_run(conn, run_id)
    if run_id is None:
        return {"snapshot_id": None, "strata": [], "honesty_note": HONESTY_NOTE}
    rows = conn.execute(
        "SELECT region, sex, age_group, population, reason FROM unestimable_strata"
        " WHERE run_id = ? ORDER BY region, sex, age_group",
        (run_id,),
    ).fetchall()
    return {
        "snapshot_id": run_id,
        "strata": [dict(r) for r in rows],
        "uncovered_population": sum(r["population"] for r in rows),
        "honesty_note": HONESTY_NOTE,
    }


def compare_runs(conn, run_a, run_b):
    """比较两次运行（修订影响）：总体/分组估计变化、权重变化、无法估计层变化。"""
    ma, mb = master_report(conn, run_a), master_report(conn, run_b)

    def delta(key):
        va, vb = ma.get(key), mb.get(key)
        if va is None or vb is None:
            return None
        return vb - va

    # 分组（按区县）估计对比
    ga = {g["group"]: g for g in group_report(conn, "region", run_a)["groups"]}
    gb = {g["group"]: g for g in group_report(conn, "region", run_b)["groups"]}
    region_rows = []
    for grp in sorted(set(ga) | set(gb)):
        a, b = ga.get(grp), gb.get(grp)
        region_rows.append({
            "group": grp,
            "satisfaction_before": a and a["weighted_mean_satisfaction"],
            "satisfaction_after": b and b["weighted_mean_satisfaction"],
            "satisfaction_delta": (
                b["weighted_mean_satisfaction"] - a["weighted_mean_satisfaction"]
                if a and b and a["weighted_mean_satisfaction"] is not None
                and b["weighted_mean_satisfaction"] is not None else None
            ),
            "sum_weight_before": a and a["sum_weight"],
            "sum_weight_after": b and b["sum_weight"],
        })

    # 记录级权重变化
    ch = conn.execute(
        "SELECT COUNT(*) AS changed, COALESCE(MAX(ABS(a.weight - b.weight)), 0) AS max_delta"
        " FROM record_weights a JOIN record_weights b ON b.record_id = a.record_id"
        " WHERE a.run_id = ? AND b.run_id = ? AND ABS(a.weight - b.weight) > 1e-9",
        (run_a, run_b),
    ).fetchone()

    # 无法估计层变化
    def unest_keys(run_id):
        return {
            (r["region"], r["sex"], r["age_group"]): r["population"]
            for r in conn.execute(
                "SELECT region, sex, age_group, population FROM unestimable_strata WHERE run_id = ?",
                (run_id,),
            )
        }
    ua, ub = unest_keys(run_a), unest_keys(run_b)
    newly_estimable = sorted(k for k in ua if k not in ub)
    newly_unestimable = sorted(k for k in ub if k not in ua)

    return {
        "from_snapshot": ma["snapshot_id"],
        "to_snapshot": mb["snapshot_id"],
        "benchmark_before": ma["benchmark_version"],
        "benchmark_after": mb["benchmark_version"],
        "satisfaction_before": ma.get("weighted_mean_satisfaction"),
        "satisfaction_after": mb.get("weighted_mean_satisfaction"),
        "satisfaction_delta": delta("weighted_mean_satisfaction"),
        "used_service_before": ma.get("weighted_used_service_ratio"),
        "used_service_after": mb.get("weighted_used_service_ratio"),
        "used_service_delta": delta("weighted_used_service_ratio"),
        "uncovered_before": ma.get("uncovered_population"),
        "uncovered_after": mb.get("uncovered_population"),
        "uncovered_delta": (
            (mb.get("uncovered_population") or 0) - (ma.get("uncovered_population") or 0)
            if ma.get("uncovered_population") is not None
            and mb.get("uncovered_population") is not None else None
        ),
        "by_region": region_rows,
        "records_weight_changed": ch["changed"],
        "max_weight_change": ch["max_delta"],
        "newly_estimable_strata": ["|".join(k) for k in newly_estimable],
        "newly_unestimable_strata": ["|".join(k) for k in newly_unestimable],
        "honesty_note": HONESTY_NOTE,
    }


# ---------------------------------------------------------------- CSV 明细

RECORD_CSV_HEADER = [
    "record_id", "respondent_id", "batch_id", "region", "sex", "age_group",
    "satisfaction", "used_service",
    "run_id", "benchmark_version", "cell_key", "merge_level",
    "n_cell", "n_pop", "raw_weight", "weight", "trimmed", "computed_at",
]

STRATUM_CSV_HEADER = [
    "cell_key", "region", "sex", "age_groups", "merge_level",
    "n_cell", "n_pop", "raw_weight", "estimable", "note", "member_strata",
]


def record_weights_csv(conn, run_id=None):
    """记录级权重明细：每条的权重依据（估计单元、单元样本量、基准人口、
    原始权重、是否修剪、基准版本、计算时间）全部可下载。"""
    run_id = _resolve_run(conn, run_id)
    if run_id is None:
        return None
    bench = conn.execute(
        "SELECT v.name FROM weight_runs r JOIN benchmark_versions v"
        " ON v.id = r.benchmark_version_id WHERE r.id = ?",
        (run_id,),
    ).fetchone()["name"]
    rows = conn.execute(
        "SELECT w.record_id, r.respondent_id, r.batch_id, r.region, r.sex, r.age_group,"
        " r.satisfaction, r.used_service, w.run_id, w.cell_key, w.merge_level,"
        " w.n_cell, w.n_pop, w.raw_weight, w.weight, w.trimmed, w.computed_at"
        " FROM record_weights w JOIN sample_records r ON r.id = w.record_id"
        " WHERE w.run_id = ? ORDER BY w.record_id",
        (run_id,),
    ).fetchall()
    import csv
    import io
    out = io.StringIO()
    wtr = csv.writer(out)
    wtr.writerow(RECORD_CSV_HEADER)
    for row in rows:
        wtr.writerow([
            row["record_id"], row["respondent_id"], row["batch_id"], row["region"],
            row["sex"], row["age_group"], row["satisfaction"], row["used_service"],
            row["run_id"], bench, row["cell_key"], row["merge_level"],
            row["n_cell"], row["n_pop"], f'{row["raw_weight"]:.6f}',
            f'{row["weight"]:.6f}', row["trimmed"], row["computed_at"],
        ])
    return out.getvalue()


def stratum_weights_csv(conn, run_id=None):
    """估计单元明细 + 无法估计层（estimable=0 并注明原因）。"""
    run_id = _resolve_run(conn, run_id)
    if run_id is None:
        return None
    import csv
    import io
    out = io.StringIO()
    wtr = csv.writer(out)
    wtr.writerow(STRATUM_CSV_HEADER)
    for row in conn.execute(
        "SELECT * FROM cell_weights WHERE run_id = ? ORDER BY region, cell_key", (run_id,)
    ):
        wtr.writerow([
            row["cell_key"], row["region"], row["sex"] or "*",
            ";".join(loads(row["age_groups"])), row["merge_level"],
            row["n_cell"], row["n_pop"], f'{row["raw_weight"]:.6f}',
            1, "", ";".join(loads(row["member_strata"])),
        ])
    for row in conn.execute(
        "SELECT * FROM unestimable_strata WHERE run_id = ? ORDER BY region, sex, age_group",
        (run_id,),
    ):
        wtr.writerow([
            f'{row["region"]}|{row["sex"]}|{row["age_group"]}', row["region"], row["sex"],
            row["age_group"], "", 0, row["population"], "", 0, row["reason"], "",
        ])
    return out.getvalue()
