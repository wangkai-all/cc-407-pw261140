"""权重引擎测试：合并阶梯、空层、极端权重、校准、增量等价性。"""

import unittest

from tests.base import WeightkitTestCase
from weightkit import weighting
from weightkit.db import loads


class TestFullRun(WeightkitTestCase):
    def test_weights_sum_to_covered_population(self):
        """校准后：权重和 == 可估计层覆盖人口；未覆盖人口单独列出。"""
        self.add_wave1()
        run_id = self.run_full()
        status, err = self.run_status(run_id)
        self.assertEqual(status, "completed", err)
        run = weighting.get_run(self.conn, run_id)
        stats = loads(run["stats_json"])
        self.assertAlmostEqual(stats["sum_weight"], stats["covered_population"], places=4)
        self.assertGreater(stats["covered_population"], 0)
        # 基准总人口 = 覆盖 + 未覆盖
        total = self.conn.execute(
            "SELECT SUM(population) AS p FROM benchmark_cells WHERE version_id = ?", (self.ver_a,)
        ).fetchone()["p"]
        self.assertAlmostEqual(
            total, stats["covered_population"] + stats["uncovered_population"], places=4
        )

    def test_unestimable_sex_group(self):
        """丹凤区·女 组合计 < min_cell_size 而男组充足 → 该组全部层无法估计。"""
        self.add_wave1()
        run_id = self.run_full()
        rows = self.conn.execute(
            "SELECT * FROM unestimable_strata WHERE run_id = ? AND region = '丹凤区'",
            (run_id,),
        ).fetchall()
        self.assertEqual(len(rows), 5)  # 5 个年龄段
        self.assertTrue(all(r["sex"] == "女" for r in rows))
        self.assertTrue(all("无法估计" in r["reason"] for r in rows))
        # 这些层的记录不进入权重
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM record_weights w JOIN sample_records r ON r.id = w.record_id"
            " WHERE w.run_id = ? AND r.region = '丹凤区' AND r.sex = '女'",
            (run_id,),
        ).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_age_merge_for_small_stratum(self):
        """苍梧区·男·75+ 只有 2 人 → 与相邻年龄段合并（merge_level=1）。"""
        self.add_wave1()
        run_id = self.run_full()
        cell = self.conn.execute(
            "SELECT * FROM cell_weights WHERE run_id = ? AND region = '苍梧区' AND sex = '男'"
            " AND age_groups LIKE '%75+%'",
            (run_id,),
        ).fetchone()
        self.assertIsNotNone(cell)
        self.assertEqual(cell["merge_level"], 1)
        self.assertGreaterEqual(cell["n_cell"], 5)
        self.assertIn("+", cell["cell_key"].split("|")[2])  # 多个年龄段合并

    def test_extreme_weight_trimmed(self):
        """白泽区·女 组样本少人口多 → 原始权重极端 → 被修剪并打标。"""
        self.add_wave1()
        run_id = self.run_full()
        run = weighting.get_run(self.conn, run_id)
        stats = loads(run["stats_json"])
        trimmed = self.conn.execute(
            "SELECT w.* FROM record_weights w JOIN sample_records r ON r.id = w.record_id"
            " WHERE w.run_id = ? AND w.trimmed = 1 AND r.region = '白泽区'",
            (run_id,),
        ).fetchall()
        self.assertTrue(len(trimmed) > 0, "白泽区应有被修剪的记录")
        for row in trimmed:
            self.assertLessEqual(row["raw_weight"], row["weight"] * 10)  # sanity
            self.assertAlmostEqual(
                row["weight"], stats["trim_hi_bound"] * stats["calibration"], places=6
            )

    def test_provenance_columns(self):
        """每条记录权重都带完整依据：基准版本、估计单元、单元样本量、基准人口、运行。"""
        self.add_wave1()
        run_id = self.run_full()
        row = self.conn.execute(
            "SELECT * FROM record_weights WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        self.assertEqual(row["benchmark_version_id"], self.ver_a)
        self.assertTrue(row["cell_key"])
        self.assertGreater(row["n_cell"], 0)
        self.assertGreater(row["n_pop"], 0)
        self.assertTrue(row["computed_at"])
        self.assertAlmostEqual(row["raw_weight"], row["n_pop"] / row["n_cell"], places=6)


class TestAffectedRun(WeightkitTestCase):
    def test_no_affected_strata_refused(self):
        """无新样本、基准未变 → 拒绝空转。"""
        self.add_wave1()
        self.run_full()
        with self.assertRaises(weighting.WeightingError):
            self.run_affected()

    def test_unaffected_records_byte_identical(self):
        """增量模式：未受影响区县的记录权重与父运行逐字节一致。"""
        self.add_wave1()
        run1 = self.run_full()
        self.add_batch2()  # 只涉及 白泽区/苍梧区/青云区
        run2 = self.run_affected()
        status, err = self.run_status(run2)
        self.assertEqual(status, "completed", err)
        affected = loads(weighting.get_run(self.conn, run2)["affected_json"])
        self.assertNotIn("丹凤区", affected["regions"])  # 批次二不涉及丹凤区
        before = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT w.record_id, w.weight FROM record_weights w"
                " JOIN sample_records s ON s.id = w.record_id"
                " WHERE w.run_id = ? AND s.region = '丹凤区'", (run1,)
            )
        }
        after = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT w.record_id, w.weight FROM record_weights w"
                " JOIN sample_records s ON s.id = w.record_id"
                " WHERE w.run_id = ? AND s.region = '丹凤区'", (run2,)
            )
        }
        self.assertEqual(before, after)

    def test_affected_equals_full_without_trimming(self):
        """不修剪时：增量重算结果应与全量重算逐记录一致（合并不跨区县 ⇒ 闭包精确）。"""
        no_trim = {"trim_lo": 0.0, "trim_hi": 1.0}
        self.add_wave1()
        run1 = self.run_full(config=no_trim)
        self.add_batch2()
        run2 = self.run_affected(config=no_trim)
        run3 = self.run_full(config=no_trim)  # 同样数据下的全量参照
        w2 = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT record_id, weight FROM record_weights WHERE run_id = ?", (run2,)
            )
        }
        w3 = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT record_id, weight FROM record_weights WHERE run_id = ?", (run3,)
            )
        }
        self.assertEqual(set(w2), set(w3))
        for rid in w2:
            self.assertAlmostEqual(w2[rid], w3[rid], places=6, msg=f"record {rid}")

    def test_benchmark_revision_marks_changed_strata(self):
        """基准换版：人口变化的层进入受影响集合。"""
        self.add_wave1()
        self.run_full()
        ver_b = self.add_benchmark_b()
        run2 = self.run_affected(version=ver_b)
        affected = loads(weighting.get_run(self.conn, run2)["affected_json"])
        self.assertIn("丹凤区", affected["regions"])  # v2026B 丹凤区人口 +18%
        self.assertTrue(len(affected["strata"]) > 0)

    def test_revision_fills_unestimable_stratum(self):
        """补报三补上丹凤区·女 → 原本无法估计的层变为可估计，未覆盖人口归零。"""
        self.add_wave1()
        run1 = self.run_full()
        stats1 = loads(weighting.get_run(self.conn, run1)["stats_json"])
        self.assertGreater(stats1["uncovered_population"], 0)
        self.add_batch3()
        run2 = self.run_affected()
        stats2 = loads(weighting.get_run(self.conn, run2)["stats_json"])
        self.assertEqual(stats2["uncovered_population"], 0)
        n_unest = self.conn.execute(
            "SELECT COUNT(*) AS c FROM unestimable_strata WHERE run_id = ?", (run2,)
        ).fetchone()["c"]
        self.assertEqual(n_unest, 0)


if __name__ == "__main__":
    unittest.main()
