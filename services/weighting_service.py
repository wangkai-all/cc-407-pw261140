#!/usr/bin/env python3
"""权重计算服务（端口 8002）：全量/增量重算，可中断、可恢复、可发布。

- 同一时刻只允许一个运行在执行（DB 状态即锁，重启后遗留 running 自动转为可恢复的 interrupted）
- 执行在后台线程，按 chunk 推进 checkpoint；中断请求通过 DB 标志传递
"""

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from weightkit import weighting
from weightkit.db import connect, init_db
from weightkit.httpserver import App, json_response, parse_json_body

PORT = int(os.environ.get("WEIGHTING_PORT", "8002"))
DB = os.environ.get("WEIGHTKIT_DB") or None  # None → weightkit.db 默认路径
app = App("weighting")
_workers = {}
_workers_lock = threading.Lock()


def _spawn_worker(run_id):
    def target():
        try:
            weighting.execute_run(DB, run_id)
        finally:
            with _workers_lock:
                _workers.pop(run_id, None)

    thread = threading.Thread(target=target, name=f"run-{run_id}", daemon=True)
    with _workers_lock:
        _workers[run_id] = thread
    thread.start()


def _any_running(conn):
    return conn.execute(
        "SELECT id FROM weight_runs WHERE status = 'running' LIMIT 1"
    ).fetchone()


@app.route("GET", "/health")
def health(params, query, body, headers):
    return {"service": "weighting", "ok": True}


@app.route("POST", "/api/runs")
def create_run(params, query, body, headers):
    """启动计算。body: {mode, benchmark_version_id, parent_run_id?, config?}"""
    try:
        payload = parse_json_body(body)
        conn = connect(DB)
        running = _any_running(conn)
        if running:
            return json_response(
                {"error": f"运行 {running['id']} 正在执行，请先等待或中断它"}, 409
            )
        run_id = weighting.create_run(
            conn,
            mode=payload.get("mode", "full"),
            benchmark_version_id=int(payload["benchmark_version_id"]),
            parent_run_id=payload.get("parent_run_id"),
            config=payload.get("config"),
        )
    except (weighting.WeightingError, KeyError, ValueError) as exc:
        return json_response({"error": str(exc)}, 400)
    _spawn_worker(run_id)
    return json_response(weighting.get_run(connect(DB), run_id), 201)


@app.route("GET", "/api/runs")
def list_runs(params, query, body, headers):
    return {"runs": weighting.list_runs(connect(DB))}


@app.route("GET", "/api/runs/{rid}")
def get_run(params, query, body, headers):
    try:
        return weighting.get_run(connect(DB), int(params["rid"]))
    except weighting.WeightingError as exc:
        return json_response({"error": str(exc)}, 404)


@app.route("POST", "/api/runs/{rid}/interrupt")
def interrupt(params, query, body, headers):
    try:
        weighting.request_interrupt(connect(DB), int(params["rid"]))
    except weighting.WeightingError as exc:
        return json_response({"error": str(exc)}, 400)
    return {"ok": True, "note": "中断请求已发出，工作线程将在当前 chunk 完成后停止"}


@app.route("POST", "/api/runs/{rid}/resume")
def resume(params, query, body, headers):
    conn = connect(DB)
    running = _any_running(conn)
    if running:
        return json_response({"error": f"运行 {running['id']} 正在执行"}, 409)
    try:
        weighting.resume_run(conn, int(params["rid"]))
    except weighting.WeightingError as exc:
        return json_response({"error": str(exc)}, 400)
    _spawn_worker(int(params["rid"]))
    return {"ok": True}


@app.route("POST", "/api/runs/{rid}/publish")
def publish(params, query, body, headers):
    try:
        weighting.publish(connect(DB), int(params["rid"]))
    except weighting.WeightingError as exc:
        return json_response({"error": str(exc)}, 400)
    return {"ok": True, "published_run_id": int(params["rid"])}


if __name__ == "__main__":
    init_db(DB)
    recovered = weighting.recover_stale_runs(connect(DB))
    if recovered:
        print(f"[weighting] {recovered} 个遗留 running 运行已标记为 interrupted，可恢复", flush=True)
    app.serve(PORT)
