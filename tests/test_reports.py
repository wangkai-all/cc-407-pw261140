"""接收校验与报表/下载测试。"""

import unittest

from tests.base import WeightkitTestCase
from weightkit import reports, weighting
from weightkit.ingest_lib import IngestError, import_benchmark_csv, import_sample_csv


class TestIngestValidation(WeightkitTestCase):
    def test_bad_csv_missing_column(self):
        with self.assertRaises(IngestError) as ctx:
            import_benchmark_csv(self.conn, "vX", "", "region,sex,population\n青云区,男,100\n")
        self.assertIn("age_group", str(ctx.exception))

    def test_bad_population_value(self):
        csv_text = "region,sex,age_group,population\n青云区,男,18-29,abc\n"
        with self.assertRaises(IngestError) as ctx:
            import_benchmark_csv(self.conn, "vX", "", csv_text)
        self.assertIn("第 2 行", str(ctx.exception))

    def test_duplicate_respondent_in_batch(self):
        csv_text = (
            "respondent_id,region,sex,age_group,satisfaction,used_service\n"
            "R1,青云区,男,18-29,4,1\nR1,青云区,男,18-29,3,0\n"
        )
        with self.assertRaises(IngestError) as ctx:
            import_sample_csv(self.conn, "b1", "", csv_text)
        self.assertIn("重复", str(ctx.exception))

    def test_satisfaction_out_of_range(self):
        csv_text = (
            "respondent_id,region,sex,age_group,satisfaction,used_service\n"
            "R1,青云区,男,18-29,9,1\n"
        )
        with self.assertRaises(IngestError):
            import_sample_csv(self.conn, "b1", "", csv_text)

    def test_failed_import_leaves_no_partial_data(self):
        """校验失败必须整体回滚，不留半个批次。"""
        csv_text = (
            "respondent_id,region,sex,age_group,satisfaction,used_service\n"
            "R1,青云区,男,18-29,4,1\nR2,青云区,男,18-29,99,1\n"
        )
        with self.assertRaises(IngestError):
            import_sample_csv(self.conn, "b1", "", csv_text)
        n = self.conn.execute("SELECT COUNT(*) AS c FROM sample_records").fetchone()["c"]
        self.assertEqual(n, 0)


class TestReportsAndDownloads(WeightkitTestCase):
    def setUp(self):
        super().setUp()
        self.add_wave1()
        self.run_id = self.run_full()
        weighting.publish(self.conn, self.run_id)

    def test_master_report_consistency(self):
        m = reports.master_report(self.conn)
        self.assertEqual(m["snapshot_id"], self.run_id)
        self.assertAlmostEqual(m["sum_weight"], m["covered_population"], places=4)
        self.assertTrue(1 <= m["weighted_mean_satisfaction"] <= 5)
        self.assertIn("样本设计", m["honesty_note"])

    def test_group_report_sums_to_master(self):
        """各分组权重和之和 == 总表权重和（同源同快照）。"""
        m = reports.master_report(self.conn)
        for dim in ("region", "sex", "age_group"):
            g = reports.group_report(self.conn, dim)
            self.assertEqual(g["snapshot_id"], m["snapshot_id"])
            self.assertAlmostEqual(
                sum(x["sum_weight"] for x in g["groups"]), m["sum_weight"], places=4
            )

    def test_unestimable_reported(self):
        u = reports.unestimable_report(self.conn)
        self.assertEqual(len(u["strata"]), 5)  # 丹凤区·女 5 个年龄段
        self.assertGreater(u["uncovered_population"], 0)
        self.assertTrue(all(s["reason"] for s in u["strata"]))

    def test_record_csv_has_provenance(self):
        text = reports.record_weights_csv(self.conn)
        lines = text.strip().splitlines()
        self.assertGreater(len(lines), 100)
        header = lines[0].split(",")
        for col in ("benchmark_version", "cell_key", "n_cell", "n_pop",
                    "raw_weight", "weight", "trimmed", "computed_at"):
            self.assertIn(col, header)
        row = lines[1].split(",")
        self.assertEqual(row[header.index("benchmark_version")], "v2026A")

    def test_stratum_csv_marks_unestimable(self):
        text = reports.stratum_weights_csv(self.conn)
        lines = text.strip().splitlines()
        header = lines[0].split(",")
        est_idx = header.index("estimable")
        note_idx = header.index("note")
        unest = [ln for ln in lines[1:] if ln.split(",")[est_idx] == "0"]
        self.assertEqual(len(unest), 5)
        self.assertTrue(all(ln.split(",")[note_idx] for ln in unest))

    def test_reports_before_any_snapshot(self):
        """全新库（无快照）时报表给出明确提示而不是崩溃。"""
        from weightkit.db import connect, init_db
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        init_db(path)
        conn = connect(path)
        m = reports.master_report(conn)
        self.assertIsNone(m["snapshot_id"])
        self.assertIn("尚未发布", m["error"])
        conn.close()
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
