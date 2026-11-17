#!/usr/bin/env python3
"""报表服务（端口 8003）：总表/分组报表、修订比较、明细下载、Web UI。

只读已发布快照（或显式指定的已完成运行），自身不产生任何权重。
总表与分组报表永远携带同一个 snapshot_id —— 不存在新旧权重混用的窗口。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from weightkit import reports, weighting
from weightkit.db import connect, init_db
from weightkit.httpserver import App, Response, json_response

PORT = int(os.environ.get("REPORT_PORT", "8003"))
DB = os.environ.get("WEIGHTKIT_DB") or None
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
app = App("report", static_dir=WEB_DIR)


@app.route("GET", "/health")
def health(params, query, body, headers):
    return {"service": "report", "ok": True}


@app.route("GET", "/api/snapshot")
def snapshot(params, query, body, headers):
    snap = weighting.current_snapshot(connect(DB))
    if not snap:
        return {"snapshot": None, "honesty_note": weighting.HONESTY_NOTE}
    return {"snapshot": snap, "honesty_note": weighting.HONESTY_NOTE}


@app.route("GET", "/api/reports/master")
def master(params, query, body, headers):
    return reports.master_report(connect(DB), query.get("run_id"))


@app.route("GET", "/api/reports/groups")
def groups(params, query, body, headers):
    try:
        return reports.group_report(connect(DB), query.get("dim", "region"), query.get("run_id"))
    except ValueError as exc:
        return json_response({"error": str(exc)}, 400)


@app.route("GET", "/api/reports/unestimable")
def unestimable(params, query, body, headers):
    return reports.unestimable_report(connect(DB), query.get("run_id"))


@app.route("GET", "/api/compare")
def compare(params, query, body, headers):
    try:
        return reports.compare_runs(connect(DB), int(query["from"]), int(query["to"]))
    except (KeyError, ValueError) as exc:
        return json_response({"error": f"参数错误: {exc}，需要 ?from=<run_id>&to=<run_id>"}, 400)


@app.route("GET", "/api/runs")
def runs(params, query, body, headers):
    """只读运行列表（供下拉选择；运行控制走 weighting 服务）。"""
    return {"runs": weighting.list_runs(connect(DB))}


@app.route("GET", "/api/download/record_weights.csv")
def download_records(params, query, body, headers):
    text = reports.record_weights_csv(connect(DB), query.get("run_id"))
    if text is None:
        return json_response({"error": "尚未发布任何快照"}, 404)
    return Response(200, text, "text/csv; charset=utf-8",
                    {"Content-Disposition": "attachment; filename=record_weights.csv"})


@app.route("GET", "/api/download/stratum_weights.csv")
def download_strata(params, query, body, headers):
    text = reports.stratum_weights_csv(connect(DB), query.get("run_id"))
    if text is None:
        return json_response({"error": "尚未发布任何快照"}, 404)
    return Response(200, text, "text/csv; charset=utf-8",
                    {"Content-Disposition": "attachment; filename=stratum_weights.csv"})


if __name__ == "__main__":
    init_db(DB)
    app.serve(PORT)
