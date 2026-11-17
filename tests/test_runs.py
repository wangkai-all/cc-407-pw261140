"""运行控制测试：中断/恢复等价性、崩溃恢复、发布一致性。"""

import threading
import time
import unittest

from tests.base import WeightkitTestCase
from weightkit import weighting
from weightkit.db import connect, loads


class TestInterruptResume(WeightkitTestCase):
    def test_interrupt_resume_equals_uninterrupted(self):
        """中断后恢复的最终权重，与一口气跑完完全一致。"""
        self.add_wave1()
        # 参照：不中断
        run_ref = self.run_full()
        # 实验：每 chunk 延迟 50ms，启动后尽快中断
        run_id = weighting.create_run(
            self.conn, "full", self.ver_a,
            config={"chunk_delay_ms": 50, "chunk_size": 2},
        )
        thread = threading.Thread(target=weighting.execute_run, args=(self.db_path, run_id))
        thread.start()
        time.sleep(0.15)
        weighting.request_interrupt(connect(self.db_path), run_id)
        thread.join(timeout=30)
        run = weighting.get_run(self.conn, run_id)
        self.assertEqual(run["status"], "interrupted")
        self.assertGreater(run["checkpoint"], 0)
        # 恢复
        weighting.resume_run(self.conn, run_id)
        weighting.execute_run(self.db_path, run_id)
        run = weighting.get_run(self.conn, run_id)
        self.assertEqual(run["status"], "completed")
        # 与参照逐记录一致
        ref = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT record_id, weight FROM record_weights WHERE run_id = ?", (run_ref,)
            )
        }
        got = {
            r["record_id"]: r["weight"]
            for r in self.conn.execute(
                "SELECT record_id, weight FROM record_weights WHERE run_id = ?", (run_id,)
            )
        }
        self.assertEqual(ref, got)

    def test_stale_running_recovered_as_interrupted(self):
        """模拟进程崩溃：running 状态遗留 → recover_stale_runs 转为可恢复。"""
        self.add_wave1()
        run_id = weighting.create_run(self.conn, "full", self.ver_a, config={})
        n = weighting.recover_stale_runs(self.conn)
        self.assertEqual(n, 1)
        run = weighting.get_run(self.conn, run_id)
        self.assertEqual(run["status"], "interrupted")
        # 可恢复并完成
        weighting.resume_run(self.conn, run_id)
        weighting.execute_run(self.db_path, run_id)
        self.assertEqual(weighting.get_run(self.conn, run_id)["status"], "completed")

    def test_cannot_interrupt_completed(self):
        self.add_wave1()
        run_id = self.run_full()
        with self.assertRaises(weighting.WeightingError):
            weighting.request_interrupt(self.conn, run_id)


class TestPublishConsistency(WeightkitTestCase):
    def test_publish_is_atomic_and_reports_follow(self):
        """发布后：总表与分组报表的 snapshot_id 同时切换（无中间态）。"""
        from weightkit import reports
        self.add_wave1()
        run1 = self.run_full()
        self.assertIsNone(weighting.current_snapshot(self.conn))
        weighting.publish(self.conn, run1)
        self.assertEqual(reports.master_report(self.conn)["snapshot_id"], run1)
        self.assertEqual(reports.group_report(self.conn, "region")["snapshot_id"], run1)
        # 第二个快照发布后，两类报表一起切换
        self.add_batch2()
        run2 = self.run_affected()
        weighting.publish(self.conn, run2)
        self.assertEqual(reports.master_report(self.conn)["snapshot_id"], run2)
        self.assertEqual(reports.group_report(self.conn, "sex")["snapshot_id"], run2)
        self.assertEqual(reports.unestimable_report(self.conn)["snapshot_id"], run2)

    def test_publish_requires_completed(self):
        self.add_wave1()
        run_id = weighting.create_run(self.conn, "full", self.ver_a, config={})
        with self.assertRaises(weighting.WeightingError):
            weighting.publish(self.conn, run_id)


class TestCompare(WeightkitTestCase):
    def test_compare_revision_impact(self):
        """①→③ 比较：丹凤区满意度变化、未覆盖人口归零、新可估计层列出。"""
        from weightkit import reports
        self.add_wave1()
        run1 = self.run_full()
        self.add_batch3()  # 补丹凤区·女
        run2 = self.run_affected()
        c = reports.compare_runs(self.conn, run1, run2)
        self.assertEqual(c["from_snapshot"], run1)
        self.assertEqual(c["to_snapshot"], run2)
        self.assertGreater(c["uncovered_before"], 0)
        self.assertEqual(c["uncovered_after"], 0)
        self.assertTrue(any(k.startswith("丹凤区|女") for k in c["newly_estimable_strata"]))
        danfeng = [r for r in c["by_region"] if r["group"] == "丹凤区"][0]
        self.assertIsNotNone(danfeng["satisfaction_delta"])
        self.assertGreater(c["records_weight_changed"], 0)


if __name__ == "__main__":
    unittest.main()
