#!/usr/bin/env python3
"""端到端演示：虚构数据 → 发布基准 → 补报 → 全量/增量重算 → 中断恢复 → 修订比较。

前置：三个服务已在运行（python3 scripts/run_all.py）。
本脚本只通过 HTTP API 操作，验证"三服务独立运行"的架构。

演示剧情：
 1. 发布基准 v2026A，接收第一波样本
 2. 全量重算（带人工延迟），中途故意中断 → 恢复 → 完成 → 发布快照①
 3. 补报批次二 → 增量重算 → 发布快照②
 4. 基准换版 v2026B + 补报批次三（补上原本无法估计的丹凤区·女）
    → 增量重算 → 发布快照③
 5. 比较 ①→③ 的修订影响，下载明细 CSV 到 data/exports/
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from weightkit import seed  # noqa: E402

INGEST = "http://localhost:8001"
WEIGHTING = "http://localhost:8002"
REPORT = "http://localhost:8003"
EXPORTS = os.path.join(ROOT, "data", "exports")


def call(base, path, method="GET", body=None, raw=False):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
            req.add_header("Content-Type", "text/csv")
        else:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            payload = resp.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} {method} {base}{path}\n{detail}")


def wait_run(run_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = call(WEIGHTING, f"/api/runs/{run_id}")
        if run["status"] in ("completed", "failed", "interrupted"):
            return run
        time.sleep(0.3)
    raise SystemExit(f"运行 {run_id} 超时未完成")


def q(params):
    return urllib.parse.urlencode(params)


def step(msg):
    print(f"\n=== {msg} " + "=" * max(0, 60 - len(msg)))


def main():
    for name, base in [("ingest", INGEST), ("weighting", WEIGHTING), ("report", REPORT)]:
        call(base, "/health")
    print("三个服务健康检查通过")

    step("1. 发布基准 v2026A + 接收第一波样本")
    r = call(INGEST, "/api/benchmarks?" + q({"name": "v2026A", "note": "2026年初版"}), "POST", seed.benchmark_csv(seed.BENCHMARK_A_NAME))
    ver_a = r["version_id"]
    print(f"基准 v2026A: id={ver_a}, 层数={r['cells']}, 总人口={r['total_population']:,.0f}")
    r = call(INGEST, "/api/samples?" + q({"source": "2026年3月第一波", "note": "首轮"}), "POST", seed.samples_csv(seed.wave1_rows()))
    print(f"第一波样本: batch={r['batch_id']}, 记录={r['inserted']}, 涉及层数={len(r['affected_strata'])}")

    step("2. 全量重算（带延迟）→ 中断 → 恢复 → 发布快照①")
    run = call(WEIGHTING, "/api/runs", "POST", {
        "mode": "full", "benchmark_version_id": ver_a,
        "config": {"chunk_delay_ms": 300, "chunk_size": 3},
    })
    run1 = run["id"]
    time.sleep(1.2)  # 让它跑几个 chunk
    call(WEIGHTING, f"/api/runs/{run1}/interrupt", "POST")
    run = wait_run(run1)
    assert run["status"] == "interrupted", run["status"]
    print(f"运行 {run1} 已中断于 checkpoint={run['checkpoint']}")
    call(WEIGHTING, f"/api/runs/{run1}/resume", "POST")
    run = wait_run(run1)
    assert run["status"] == "completed", run["status"]
    stats = json.loads(run["stats_json"])
    print(f"恢复后完成: 覆盖人口={stats['covered_population']:,.0f}, "
          f"未覆盖={stats['uncovered_population']:,.0f}, 修剪记录={stats['n_trimmed']}")
    call(WEIGHTING, f"/api/runs/{run1}/publish", "POST")
    m = call(REPORT, "/api/reports/master")
    print(f"快照①已发布: 加权满意度={m['weighted_mean_satisfaction']:.4f}, "
          f"权重和={m['sum_weight']:,.0f}")

    step("3. 补报批次二 → 增量重算 → 发布快照②")
    r = call(INGEST, "/api/samples?" + q({"source": "2026年6月补报", "note": "第二批"}), "POST", seed.samples_csv(seed.batch2_rows()))
    print(f"批次二: batch={r['batch_id']}, 记录={r['inserted']}")
    run = call(WEIGHTING, "/api/runs", "POST", {"mode": "affected", "benchmark_version_id": ver_a})
    run2 = run["id"]
    run = wait_run(run2)
    assert run["status"] == "completed", run["status"]
    affected = json.loads(run["affected_json"])
    print(f"增量重算完成: 受影响层 {len(affected['strata'])} 个，波及区县 {affected['regions']}")
    call(WEIGHTING, f"/api/runs/{run2}/publish", "POST")
    print("快照②已发布")

    step("4. 基准换版 v2026B + 补报批次三 → 增量重算 → 发布快照③")
    r = call(INGEST, "/api/benchmarks?" + q({"name": "v2026B", "note": "2026年人口修订"}), "POST", seed.benchmark_csv(seed.BENCHMARK_B_NAME))
    ver_b = r["version_id"]
    print(f"基准 v2026B: id={ver_b}, 总人口={r['total_population']:,.0f}")
    r = call(INGEST, "/api/samples?" + q({"source": "2026年9月补报", "note": "第三批(补丹凤区女)"}), "POST", seed.samples_csv(seed.batch3_rows()))
    print(f"批次三: batch={r['batch_id']}, 记录={r['inserted']}")
    run = call(WEIGHTING, "/api/runs", "POST", {"mode": "affected", "benchmark_version_id": ver_b})
    run3 = run["id"]
    run = wait_run(run3)
    assert run["status"] == "completed", run["status"]
    call(WEIGHTING, f"/api/runs/{run3}/publish", "POST")
    print("快照③已发布")

    step("5. 修订比较 ①→③ + 下载明细")
    c = call(REPORT, f"/api/compare?from={run1}&to={run3}")
    print(f"加权满意度: {c['satisfaction_before']:.4f} → {c['satisfaction_after']:.4f} "
          f"(Δ{c['satisfaction_delta']:+.4f})")
    print(f"未覆盖人口: {c['uncovered_before']:,.0f} → {c['uncovered_after']:,.0f}")
    print(f"权重变化记录数: {c['records_weight_changed']}, 最大单条变化: {c['max_weight_change']:.3f}")
    if c["newly_estimable_strata"]:
        print(f"新变为可估计的层: {', '.join(c['newly_estimable_strata'])}")

    os.makedirs(EXPORTS, exist_ok=True)
    for name in ("record_weights", "stratum_weights"):
        data = call(REPORT, f"/api/download/{name}.csv", raw=True)
        path = os.path.join(EXPORTS, f"{name}.csv")
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"已下载 {path}（{len(data):,} 字节）")

    print("\n演示完成。打开 http://localhost:8003/ 查看 Web 界面。")


if __name__ == "__main__":
    main()
