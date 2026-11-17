"""权重计算引擎：事后分层加权 + 层合并 + 极端权重修剪 + 校准。

设计要点（与 README 的取舍说明对应）：

1. 估计单元与合并阶梯（不跨区县）：
   - L0 原始层 (region, sex, age_group)，样本量 >= min_cell_size 直接估计；
   - L1 同 (region, sex) 内向相邻年龄段合并（贪心，直到每个单元样本量达标）；
   - L2 同 region 内合并性别后再按年龄合并 —— 仅当该 region 内**所有**性别组
     各自都不达标但合并后达标时才启用；若某一性别组充足而另一组不足，
     不足组的层判为"无法估计"（不为凑数拉低充足组的估计质量）；
   - 仍不达标（整个 region 样本不足）→ 无法估计，人口计入未覆盖并明确列出。
   合并不跨 region，因此"增量重算"的最小波及范围就是 region —— 这是
   全量/增量结果可对照验证的基础。

2. 极端权重：按记录级原始权重的分位数（默认 2% / 98%）截断并打标，
   再做一次整体校准，使权重和 = 可估计层覆盖人口。

3. 增量重算（affected）：只重算受影响 region，未受影响 region 的记录权重
   从父运行原样复制（逐字节一致）；修剪边界冻结为父运行的值，校准只对
   受影响部分做残差校准。代价：全局修剪分位会随数据漂移而过时，
   需定期全量重算（stats 中记录自上次全量以来的增量次数）。

4. 断点恢复：计划阶段（单元划分、修剪边界、校准因子）一个事务落库；
   记录级权重按 chunk 写入，每 chunk 一个事务并推进 checkpoint；
   中断/崩溃后从 checkpoint 继续，结果与不中断完全一致。
"""

import time

from .db import connect, dumps, loads, utcnow

DEFAULT_CONFIG = {
    "min_cell_size": 5,      # 层最小样本量
    "trim_lo": 0.02,         # 权重下截断分位（0 = 不截断）
    "trim_hi": 0.98,         # 权重上截断分位（1 = 不截断）
    "chunk_size": 3,         # 每事务处理的估计单元数（断点粒度）
    "chunk_delay_ms": 0,     # 每 chunk 人工延迟（演示中断用；生产为真实计算耗时）
}

HONESTY_NOTE = (
    "本结果仅基于给定样本设计与所发布基准的事后分层加权，结论只适用于该样本设计；"
    "无法估计层的人口未进入估计，不对总体代表性作额外主张。"
)


class WeightingError(ValueError):
    pass


# ---------------------------------------------------------------- 基础工具

def stratum_key(region, sex, age_group):
    return f"{region}|{sex}|{age_group}"


def age_order(label):
    """从年龄段标签解析排序键：'18-29'→18, '75+'→75，无法解析排最后。"""
    digits = ""
    for ch in str(label):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return (int(digits) if digits else 9999, str(label))


def _weighted_quantile(pairs, q):
    """pairs: 按值排序的 [(value, count)]，求记录级分位数。"""
    total = sum(c for _, c in pairs)
    if total <= 0:
        return None
    threshold = q * total
    acc = 0
    for value, count in pairs:
        acc += count
        if acc >= threshold:
            return value
    return pairs[-1][0]


# ---------------------------------------------------------------- 层划分

def load_strata(conn, benchmark_version_id):
    """基准层 ∪ 样本层 → {key: {region, sex, age_group, pop, n, in_benchmark}}"""
    strata = {}
    for row in conn.execute(
        "SELECT region, sex, age_group, population FROM benchmark_cells WHERE version_id = ?",
        (benchmark_version_id,),
    ):
        key = stratum_key(row["region"], row["sex"], row["age_group"])
        strata[key] = {
            "region": row["region"], "sex": row["sex"], "age_group": row["age_group"],
            "pop": row["population"], "n": 0, "in_benchmark": True,
        }
    for row in conn.execute(
        "SELECT region, sex, age_group, COUNT(*) AS c FROM sample_records GROUP BY 1, 2, 3"
    ):
        key = stratum_key(row["region"], row["sex"], row["age_group"])
        if key in strata:
            strata[key]["n"] = row["c"]
        else:
            strata[key] = {
                "region": row["region"], "sex": row["sex"], "age_group": row["age_group"],
                "pop": 0.0, "n": row["c"], "in_benchmark": False,
            }
    return strata


def _age_merge(items, min_n):
    """同组内按年龄顺序贪心合并相邻层，使每个单元样本量 >= min_n。

    items: 按 age_order 排序的层列表。返回 [ [层, ...], ... ]；
    若整组合计仍不足，返回单个包含全部层的单元（由调用方升级处理）。
    """
    cells, cur, cur_n = [], [], 0
    for it in items:
        cur.append(it)
        cur_n += it["n"]
        if cur_n >= min_n:
            cells.append(cur)
            cur, cur_n = [], 0
    if cur:
        if cells:
            cells[-1].extend(cur)      # 尾部不足 → 并入前一个单元
        else:
            cells.append(cur)          # 整组不足 → 整体一个单元，调用方决定
    return cells


def _make_cell(members, region, sex, level):
    ages = sorted({m["age_group"] for m in members}, key=age_order)
    return {
        "cell_key": f"{region}|{sex or '*'}|{'+'.join(ages)}",
        "region": region,
        "sex": sex,
        "age_groups": ages,
        "merge_level": level,
        "n_cell": sum(m["n"] for m in members),
        "n_pop": sum(m["pop"] for m in members),
        "member_strata": sorted(stratum_key(m["region"], m["sex"], m["age_group"]) for m in members),
    }


def assign_cells(strata, min_cell_size):
    """层 → 估计单元 + 无法估计层。确定性算法，同样输入必得同样结果。"""
    cells, unestimable = [], []

    # 基准人口为 0 或层不在基准中的样本：无法加权，直接列入无法估计
    workable = {}
    for key, s in sorted(strata.items()):
        if not s["in_benchmark"]:
            unestimable.append({**s, "reason": "层不在所选基准版本中，无法加权"})
        elif s["pop"] <= 0:
            unestimable.append({**s, "reason": "基准人口为 0，无法加权"})
        else:
            workable[key] = s

    regions = sorted({s["region"] for s in workable.values()})
    for region in regions:
        in_region = [s for s in workable.values() if s["region"] == region]
        by_sex = {}
        for s in in_region:
            by_sex.setdefault(s["sex"], []).append(s)
        for lst in by_sex.values():
            lst.sort(key=lambda s: age_order(s["age_group"]))
        group_n = {sex: sum(s["n"] for s in lst) for sex, lst in by_sex.items()}
        sexes = sorted(by_sex)
        sufficient = [sx for sx in sexes if group_n[sx] >= min_cell_size]
        deficient = [sx for sx in sexes if group_n[sx] < min_cell_size]

        def emit_sex_group(sex):
            for members in _age_merge(by_sex[sex], min_cell_size):
                level = 0 if len(members) == 1 else 1
                cells.append(_make_cell(members, region, sex, level))

        if not deficient:
            for sex in sexes:
                emit_sex_group(sex)
        elif not sufficient:
            # 所有性别组各自都不达标：尝试整区合并性别
            if sum(group_n.values()) >= min_cell_size:
                pooled = sorted(in_region, key=lambda s: (age_order(s["age_group"]), s["sex"]))
                for members in _age_merge(pooled, min_cell_size):
                    cells.append(_make_cell(members, region, None, 2))
            else:
                for s in in_region:
                    unestimable.append({**s, "reason": f"{region} 样本合计 {sum(group_n.values())} < 最小层样本量 {min_cell_size}，无法估计"})
        else:
            # 有的组充足、有的组不足：充足组正常估计，不足组明确判为无法估计
            for sex in sufficient:
                emit_sex_group(sex)
            for sex in deficient:
                for s in by_sex[sex]:
                    unestimable.append({
                        **s,
                        "reason": (
                            f"({region}, {sex}) 组样本合计 {group_n[sex]} < 最小层样本量 {min_cell_size}；"
                            "按既定政策不与充足的性别组混合，判为无法估计"
                        ),
                    })
    return cells, unestimable


# ---------------------------------------------------------------- 运行生命周期

def get_run(conn, run_id):
    row = conn.execute("SELECT * FROM weight_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise WeightingError(f"运行 {run_id} 不存在")
    return dict(row)


def list_runs(conn):
    rows = conn.execute(
        "SELECT r.*, v.name AS benchmark_name,"
        "        (SELECT run_id FROM current_snapshot WHERE id = 1) AS published_run_id"
        " FROM weight_runs r JOIN benchmark_versions v ON v.id = r.benchmark_version_id"
        " ORDER BY r.id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        plan = loads(d.get("plan_json")) or []
        d["progress_total"] = len(plan)
        d["progress_done"] = min(d["checkpoint"], len(plan))
        d["published"] = (d.pop("published_run_id") == d["id"])
        out.append(d)
    return out


def _benchmark_diff(conn, version_a, version_b):
    """两版本基准间人口有变化的层 key 列表。"""
    def load(v):
        return {
            stratum_key(r["region"], r["sex"], r["age_group"]): r["population"]
            for r in conn.execute(
                "SELECT region, sex, age_group, population FROM benchmark_cells WHERE version_id = ?", (v,)
            )
        }
    a, b = load(version_a), load(version_b)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def compute_affected(conn, parent, benchmark_version_id):
    """受影响层 = 父运行之后新到样本所在层 ∪ 基准换版中人口变化的层。"""
    pstats = loads(parent["stats_json"]) or {}
    max_batch = pstats.get("max_batch_id", 0)
    new_rows = conn.execute(
        "SELECT DISTINCT region, sex, age_group FROM sample_records WHERE batch_id > ?",
        (max_batch,),
    ).fetchall()
    affected = {stratum_key(r["region"], r["sex"], r["age_group"]) for r in new_rows}
    if parent["benchmark_version_id"] != benchmark_version_id:
        affected |= set(_benchmark_diff(conn, parent["benchmark_version_id"], benchmark_version_id))
    regions = sorted({k.split("|")[0] for k in affected})
    return sorted(affected), regions


def create_run(conn, mode, benchmark_version_id, parent_run_id=None, config=None):
    """创建运行（不执行）。affected 模式在此计算受影响范围；无受影响层则拒绝。"""
    if not conn.execute("SELECT 1 FROM benchmark_versions WHERE id = ?", (benchmark_version_id,)).fetchone():
        raise WeightingError(f"基准版本 {benchmark_version_id} 不存在")
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if cfg["min_cell_size"] < 1:
        raise WeightingError("min_cell_size 必须 >= 1")
    if not (0 <= cfg["trim_lo"] <= cfg["trim_hi"] <= 1):
        raise WeightingError("trim 分位需满足 0 <= trim_lo <= trim_hi <= 1")

    affected_payload = None
    if mode == "affected":
        if parent_run_id is None:
            row = conn.execute("SELECT run_id FROM current_snapshot WHERE id = 1").fetchone()
            if row:
                parent_run_id = row["run_id"]
            else:
                # 无已发布快照时退而使用最近完成的运行（权重依据链仍然完整）
                row = conn.execute(
                    "SELECT id FROM weight_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                parent_run_id = row["id"] if row else None
        if parent_run_id is None:
            raise WeightingError("增量重算需要父运行（当前无已发布快照，请先全量重算）")
        parent = get_run(conn, parent_run_id)
        if parent["status"] != "completed":
            raise WeightingError(f"父运行 {parent_run_id} 状态为 {parent['status']}，不能作为增量基础")
        strata, regions = compute_affected(conn, parent, benchmark_version_id)
        if not strata:
            raise WeightingError("没有受影响的层（无新样本、基准人口无变化），无需重算")
        pstats = loads(parent["stats_json"]) or {}
        affected_payload = {
            "strata": strata,
            "regions": regions,
            # 冻结父运行的修剪边界：增量模式不重算全局分位（取舍见模块 docstring）
            "frozen_trim_lo": pstats.get("trim_lo_bound"),
            "frozen_trim_hi": pstats.get("trim_hi_bound"),
        }
    elif mode != "full":
        raise WeightingError(f"未知模式: {mode}")

    with conn:
        cur = conn.execute(
            "INSERT INTO weight_runs(mode, benchmark_version_id, parent_run_id, status,"
            " config_json, affected_json, created_at, heartbeat_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mode, benchmark_version_id, parent_run_id, "running",
             dumps(cfg), dumps(affected_payload) if affected_payload else None,
             utcnow(), utcnow()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------- 执行（工作线程内调用）

def execute_run(db_path, run_id):
    """执行（或断点续执行）一个运行。可重入：任何中断后再次调用都会从断点继续。"""
    conn = connect(db_path)
    try:
        run = get_run(conn, run_id)
        if run["status"] not in ("running",):
            return  # 已中断/完成/失败，由调用方决定下一步
        if run["mode"] == "affected" and not run["base_copied"]:
            _base_copy(conn, run)
            run = get_run(conn, run_id)
        if not loads(run["plan_json"]):
            _build_plan(conn, run)
            run = get_run(conn, run_id)
        _chunk_loop(conn, run)
    except Exception as exc:  # 失败要落库，不能无声
        with conn:
            conn.execute(
                "UPDATE weight_runs SET status = 'failed', error = ?, heartbeat_at = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}", utcnow(), run_id),
            )
    finally:
        conn.close()


def _base_copy(conn, run):
    """affected 模式：把父运行中未受影响 region 的结果原样复制到本运行。"""
    parent_id = run["parent_run_id"]
    regions = loads(run["affected_json"])["regions"]
    ph = ",".join("?" for _ in regions)
    with conn:
        conn.execute(
            f"INSERT INTO cell_weights(run_id, cell_key, region, sex, age_groups, merge_level,"
            f" n_cell, n_pop, raw_weight, member_strata)"
            f" SELECT ?, cell_key, region, sex, age_groups, merge_level, n_cell, n_pop,"
            f" raw_weight, member_strata FROM cell_weights"
            f" WHERE run_id = ? AND region NOT IN ({ph})",
            (run["id"], parent_id, *regions),
        )
        conn.execute(
            f"INSERT INTO unestimable_strata(run_id, region, sex, age_group, population, reason)"
            f" SELECT ?, region, sex, age_group, population, reason FROM unestimable_strata"
            f" WHERE run_id = ? AND region NOT IN ({ph})",
            (run["id"], parent_id, *regions),
        )
        conn.execute(
            f"INSERT INTO record_weights(run_id, record_id, cell_key, merge_level, n_cell, n_pop,"
            f" raw_weight, weight, trimmed, benchmark_version_id, computed_at)"
            f" SELECT ?, record_id, cell_key, merge_level, n_cell, n_pop, raw_weight, weight,"
            f" trimmed, benchmark_version_id, computed_at FROM record_weights"
            f" WHERE run_id = ? AND record_id IN"
            f" (SELECT id FROM sample_records WHERE region NOT IN ({ph}))",
            (run["id"], parent_id, *regions),
        )
        conn.execute("UPDATE weight_runs SET base_copied = 1, heartbeat_at = ? WHERE id = ?",
                     (utcnow(), run["id"]))


def _build_plan(conn, run):
    """计划阶段：划分估计单元、确定修剪边界与校准因子，一个事务落库。"""
    run_id = run["id"]
    cfg = loads(run["config_json"])
    strata = load_strata(conn, run["benchmark_version_id"])
    cells, unestimable = assign_cells(strata, cfg["min_cell_size"])

    warnings = []
    if run["mode"] == "affected":
        payload = loads(run["affected_json"])
        regions = set(payload["regions"])
        cells = [c for c in cells if c["region"] in regions]
        unestimable = [u for u in unestimable if u["region"] in regions]
        lo_bound, hi_bound = payload.get("frozen_trim_lo"), payload.get("frozen_trim_hi")
    else:
        pairs = sorted(
            ((c["n_pop"] / c["n_cell"], c["n_cell"]) for c in cells if c["n_cell"] > 0),
            key=lambda p: p[0],
        )
        lo_bound = _weighted_quantile(pairs, cfg["trim_lo"]) if cfg["trim_lo"] > 0 else None
        hi_bound = _weighted_quantile(pairs, cfg["trim_hi"]) if cfg["trim_hi"] < 1 else None

    def clip(w):
        if lo_bound is not None:
            w = max(w, lo_bound)
        if hi_bound is not None:
            w = min(w, hi_bound)
        return w

    for c in cells:
        c["raw_weight"] = c["n_pop"] / c["n_cell"] if c["n_cell"] > 0 else 0.0

    if run["mode"] == "affected":
        # 残差校准：未受影响部分权重原样保留，受影响部分校准到剩余人口
        copied = conn.execute(
            "SELECT COALESCE(SUM(n_pop), 0) AS pop FROM cell_weights WHERE run_id = ?", (run_id,)
        ).fetchone()["pop"]
        w_unaffected = conn.execute(
            "SELECT COALESCE(SUM(weight), 0) AS w FROM record_weights WHERE run_id = ?", (run_id,)
        ).fetchone()["w"]
        covered_total = copied + sum(c["n_pop"] for c in cells)
        s_affected = sum(clip(c["raw_weight"]) * c["n_cell"] for c in cells)
        residual = covered_total - w_unaffected
        if s_affected <= 0:
            calibration, warnings = 1.0, warnings + ["受影响部分无记录权重，未做校准"]
        else:
            calibration = residual / s_affected
            if calibration <= 0 or calibration > 10:
                warnings.append(f"校准因子异常（{calibration:.4f}），建议尽快全量重算")
    else:
        covered_total = sum(c["n_pop"] for c in cells)
        clipped = sum(clip(c["raw_weight"]) * c["n_cell"] for c in cells)
        calibration = covered_total / clipped if clipped > 0 else 1.0

    max_batch = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM sample_batches").fetchone()["m"]
    parent = get_run(conn, run["parent_run_id"]) if run["parent_run_id"] else None
    incremental_since_full = 0
    if run["mode"] == "affected" and parent:
        incremental_since_full = (loads(parent["stats_json"]) or {}).get("incremental_since_full", 0) + 1

    stats = {
        "covered_population": covered_total,
        "trim_lo_bound": lo_bound,
        "trim_hi_bound": hi_bound,
        "calibration": calibration,
        "max_batch_id": max_batch,
        "incremental_since_full": incremental_since_full,
        "warnings": warnings,
        "honesty_note": HONESTY_NOTE,
    }
    plan = [c["cell_key"] for c in cells]
    now = utcnow()
    with conn:
        conn.executemany(
            "INSERT INTO cell_weights(run_id, cell_key, region, sex, age_groups, merge_level,"
            " n_cell, n_pop, raw_weight, member_strata) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, c["cell_key"], c["region"], c["sex"], dumps(c["age_groups"]),
              c["merge_level"], c["n_cell"], c["n_pop"], c["raw_weight"],
              dumps(c["member_strata"])) for c in cells],
        )
        conn.executemany(
            "INSERT INTO unestimable_strata(run_id, region, sex, age_group, population, reason)"
            " VALUES (?,?,?,?,?,?)",
            [(run_id, u["region"], u["sex"], u["age_group"], u["pop"], u["reason"])
             for u in unestimable],
        )
        conn.execute(
            "UPDATE weight_runs SET plan_json = ?, stats_json = ?, heartbeat_at = ? WHERE id = ?",
            (dumps(plan), dumps(stats), now, run_id),
        )


def _chunk_loop(conn, run):
    """记录级权重按 chunk 写入；每 chunk 一个事务并推进 checkpoint。"""
    run_id = run["id"]
    cfg = loads(run["config_json"])
    stats = loads(run["stats_json"])
    plan = loads(run["plan_json"])
    lo_b, hi_b = stats.get("trim_lo_bound"), stats.get("trim_hi_bound")
    calib = stats.get("calibration", 1.0)
    bench_ver = run["benchmark_version_id"]
    chunk_size = max(1, cfg["chunk_size"])
    delay = max(0, cfg["chunk_delay_ms"]) / 1000.0

    i = run["checkpoint"]
    while i < len(plan):
        flag = conn.execute(
            "SELECT interrupt_requested FROM weight_runs WHERE id = ?", (run_id,)
        ).fetchone()["interrupt_requested"]
        if flag:
            with conn:
                conn.execute(
                    "UPDATE weight_runs SET status = 'interrupted', heartbeat_at = ? WHERE id = ?",
                    (utcnow(), run_id),
                )
            return
        keys = plan[i:i + chunk_size]
        now = utcnow()
        with conn:
            for cell_key in keys:
                cell = conn.execute(
                    "SELECT * FROM cell_weights WHERE run_id = ? AND cell_key = ?",
                    (run_id, cell_key),
                ).fetchone()
                raw = cell["raw_weight"]
                w = raw
                if lo_b is not None:
                    w = max(w, lo_b)
                if hi_b is not None:
                    w = min(w, hi_b)
                trimmed = 1 if w != raw else 0
                w *= calib
                members = loads(cell["member_strata"])
                rec_ids = []
                for m in members:
                    r, s, a = m.split("|")
                    rec_ids.extend(
                        row["id"] for row in conn.execute(
                            "SELECT id FROM sample_records WHERE region = ? AND sex = ? AND age_group = ?",
                            (r, s, a),
                        )
                    )
                conn.executemany(
                    "INSERT INTO record_weights(run_id, record_id, cell_key, merge_level, n_cell,"
                    " n_pop, raw_weight, weight, trimmed, benchmark_version_id, computed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(run_id, rid, cell_key, cell["merge_level"], cell["n_cell"], cell["n_pop"],
                      raw, w, trimmed, bench_ver, now) for rid in rec_ids],
                )
            conn.execute(
                "UPDATE weight_runs SET checkpoint = ?, heartbeat_at = ? WHERE id = ?",
                (i + len(keys), utcnow(), run_id),
            )
        i += len(keys)
        if delay:
            time.sleep(delay)
    _finalize(conn, run_id)


def _finalize(conn, run_id):
    agg = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(trimmed), 0) AS n_trimmed,"
        " COALESCE(SUM(weight), 0) AS sum_w FROM record_weights WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    uncovered = conn.execute(
        "SELECT COALESCE(SUM(population), 0) AS p FROM unestimable_strata WHERE run_id = ?",
        (run_id,),
    ).fetchone()["p"]
    run = get_run(conn, run_id)
    stats = loads(run["stats_json"]) or {}
    stats.update({
        "n_records_weighted": agg["n"],
        "n_trimmed": agg["n_trimmed"],
        "sum_weight": agg["sum_w"],
        "uncovered_population": uncovered,
    })
    with conn:
        conn.execute(
            "UPDATE weight_runs SET status = 'completed', finished_at = ?,"
            " stats_json = ?, heartbeat_at = ? WHERE id = ?",
            (utcnow(), dumps(stats), utcnow(), run_id),
        )


# ---------------------------------------------------------------- 控制操作

def request_interrupt(conn, run_id):
    run = get_run(conn, run_id)
    if run["status"] != "running":
        raise WeightingError(f"运行 {run_id} 状态为 {run['status']}，不能中断")
    with conn:
        conn.execute("UPDATE weight_runs SET interrupt_requested = 1 WHERE id = ?", (run_id,))


def resume_run(conn, run_id):
    run = get_run(conn, run_id)
    if run["status"] != "interrupted":
        raise WeightingError(f"运行 {run_id} 状态为 {run['status']}，不能恢复")
    with conn:
        conn.execute(
            "UPDATE weight_runs SET status = 'running', interrupt_requested = 0,"
            " heartbeat_at = ? WHERE id = ?",
            (utcnow(), run_id),
        )


def recover_stale_runs(conn):
    """服务启动时调用：上次进程退出时遗留的 running 一律标记为 interrupted（可恢复）。"""
    with conn:
        cur = conn.execute(
            "UPDATE weight_runs SET status = 'interrupted' WHERE status = 'running'"
        )
    return cur.rowcount


def publish(conn, run_id):
    """原子切换当前快照：同一事务内替换单行指针，报表永远不会读到中间态。"""
    run = get_run(conn, run_id)
    if run["status"] != "completed":
        raise WeightingError(f"运行 {run_id} 状态为 {run['status']}，只有 completed 才能发布")
    with conn:
        conn.execute("DELETE FROM current_snapshot WHERE id = 1")
        conn.execute(
            "INSERT INTO current_snapshot(id, run_id, published_at) VALUES (1, ?, ?)",
            (run_id, utcnow()),
        )


def current_snapshot(conn):
    row = conn.execute(
        "SELECT s.run_id, s.published_at, r.mode, r.benchmark_version_id, v.name AS benchmark_name"
        " FROM current_snapshot s"
        " JOIN weight_runs r ON r.id = s.run_id"
        " JOIN benchmark_versions v ON v.id = r.benchmark_version_id"
        " WHERE s.id = 1"
    ).fetchone()
    return dict(row) if row else None
