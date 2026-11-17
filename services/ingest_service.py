#!/usr/bin/env python3
"""样本接收服务（端口 8001）：发布基准版本、接收补报样本批次。

职责边界：只校验和入库，绝不计算权重 —— 权重由 weighting_service 独立产出。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from weightkit.db import connect, init_db
from weightkit.httpserver import App, json_response, parse_text_body
from weightkit.ingest_lib import (
    IngestError,
    import_benchmark_csv,
    import_sample_csv,
    list_batches,
    list_benchmarks,
)

PORT = int(os.environ.get("INGEST_PORT", "8001"))
app = App("ingest")


@app.route("GET", "/health")
def health(params, query, body, headers):
    return {"service": "ingest", "ok": True}


@app.route("POST", "/api/benchmarks")
def post_benchmark(params, query, body, headers):
    """发布基准版本。query: name, note；body: CSV(region,sex,age_group,population)"""
    try:
        version_id, n_cells, total_pop = import_benchmark_csv(
            connect(), query.get("name", ""), query.get("note", ""), parse_text_body(body)
        )
    except IngestError as exc:
        return json_response({"error": str(exc)}, 400)
    return json_response(
        {"version_id": version_id, "cells": n_cells, "total_population": total_pop}, 201
    )


@app.route("GET", "/api/benchmarks")
def get_benchmarks(params, query, body, headers):
    return {"benchmarks": list_benchmarks(connect())}


@app.route("GET", "/api/benchmarks/{vid}/cells")
def get_benchmark_cells(params, query, body, headers):
    rows = connect().execute(
        "SELECT region, sex, age_group, population FROM benchmark_cells"
        " WHERE version_id = ? ORDER BY region, sex, age_group",
        (params["vid"],),
    ).fetchall()
    return {"version_id": int(params["vid"]), "cells": [dict(r) for r in rows]}


@app.route("POST", "/api/samples")
def post_samples(params, query, body, headers):
    """接收补报批次。query: source, note；body: CSV(respondent_id,region,sex,age_group,satisfaction,used_service)"""
    try:
        batch_id, n, strata = import_sample_csv(
            connect(), query.get("source", ""), query.get("note", ""), parse_text_body(body)
        )
    except IngestError as exc:
        return json_response({"error": str(exc)}, 400)
    return json_response(
        {"batch_id": batch_id, "inserted": n, "affected_strata": strata}, 201
    )


@app.route("GET", "/api/batches")
def get_batches(params, query, body, headers):
    return {"batches": list_batches(connect())}


if __name__ == "__main__":
    init_db()
    app.serve(PORT)
