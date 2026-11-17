"""测试公共基座：临时数据库 + 虚构数据装载。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from weightkit import seed  # noqa: E402
from weightkit.db import connect, init_db  # noqa: E402
from weightkit.ingest_lib import import_benchmark_csv, import_sample_csv  # noqa: E402

FAST = {"chunk_delay_ms": 0, "chunk_size": 5}


class WeightkitTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # 让 connect 自己建
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.ver_a, _, _ = import_benchmark_csv(
            self.conn, "v2026A", "", seed.benchmark_csv(seed.BENCHMARK_A_NAME)
        )

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def add_wave1(self):
        return import_sample_csv(self.conn, "wave1", "", seed.samples_csv(seed.wave1_rows()))

    def add_batch2(self):
        return import_sample_csv(self.conn, "batch2", "", seed.samples_csv(seed.batch2_rows()))

    def add_batch3(self):
        return import_sample_csv(self.conn, "batch3", "", seed.samples_csv(seed.batch3_rows()))

    def add_benchmark_b(self):
        return import_benchmark_csv(
            self.conn, "v2026B", "", seed.benchmark_csv(seed.BENCHMARK_B_NAME)
        )[0]

    def run_full(self, version=None, config=None):
        from weightkit import weighting
        run_id = weighting.create_run(
            self.conn, "full", version or self.ver_a, config={**FAST, **(config or {})}
        )
        weighting.execute_run(self.db_path, run_id)
        return run_id

    def run_affected(self, version=None, config=None):
        from weightkit import weighting
        run_id = weighting.create_run(
            self.conn, "affected", version or self.ver_a, config={**FAST, **(config or {})}
        )
        weighting.execute_run(self.db_path, run_id)
        return run_id

    def run_status(self, run_id):
        row = self.conn.execute(
            "SELECT status, error FROM weight_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row["status"], row["error"]
