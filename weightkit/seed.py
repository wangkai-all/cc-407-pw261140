"""虚构演示数据生成（确定性随机，便于测试与复现）。

场景：虚构城市"云澜市"的社区服务满意度调查。
- 4 个区县 × 2 性别 × 5 年龄段 = 40 个层
- 基准 v2026A → 换版 v2026B（丹凤区人口流入、青云区老龄化）
- 样本分波次到达：第一波 + 两次补报
- 故意构造的边界情形：
  * 丹凤区·女 各年龄段样本合计 < 最小层样本量 → 第一波时无法估计
  * 白泽区·女 组内样本少但人口多 → 极端权重 → 触发修剪
  * 苍梧区·男·75+ 样本 2 人 → 触发相邻年龄合并
  * 补报二补上丹凤区·女 → 原本无法估计的层变为可估计（修订影响）
所有数据均为虚构，统计结论只针对本样本设计，不代表任何真实总体。
"""

import csv
import io
import random

REGIONS = ["青云区", "白泽区", "苍梧区", "丹凤区"]
SEXES = ["男", "女"]
AGES = ["18-29", "30-44", "45-59", "60-74", "75+"]

BENCHMARK_A_NAME = "v2026A"
BENCHMARK_B_NAME = "v2026B"

# 第一波各 (region, sex) 组的样本量基调；丹凤区·女 故意不足
_WAVE1_GROUP_N = {
    ("青云区", "男"): 90, ("青云区", "女"): 95,
    ("白泽区", "男"): 80, ("白泽区", "女"): 8,    # 组总量小、人口多 → 极端权重
    ("苍梧区", "男"): 70, ("苍梧区", "女"): 75,
    ("丹凤区", "男"): 60, ("丹凤区", "女"): 2,   # < min_cell_size → 无法估计
}

# 补报一：常规补充；补报二：重点补丹凤区·女（让无法估计的层变得可估计）
_BATCH2_GROUP_N = {("白泽区", "女"): 12, ("苍梧区", "男"): 10, ("青云区", "女"): 8}
_BATCH3_GROUP_N = {("丹凤区", "女"): 9, ("丹凤区", "男"): 4}


def _age_split(rng, total):
    """把组样本量按大致年龄结构拆到 5 个年龄段。"""
    weights = [0.24, 0.26, 0.22, 0.17, 0.11]
    counts = [max(1, round(total * w)) for w in weights]
    # 修正凑整误差
    while sum(counts) > total:
        counts[counts.index(max(counts))] -= 1
    while sum(counts) < total:
        counts[rng.randrange(len(counts))] += 1
    return counts


def benchmark_cells(version):
    """返回 [(region, sex, age_group, population), ...]。"""
    rng = random.Random(20260101 if version == BENCHMARK_A_NAME else 20260801)
    cells = []
    for region in REGIONS:
        for sex in SEXES:
            for age in AGES:
                base = rng.randint(7000, 26000)
                if region == "丹凤区":
                    base = int(base * 0.55)          # 小区县
                if age == "75+":
                    base = int(base * 0.6)
                if version == BENCHMARK_B_NAME:
                    # 换版：丹凤区人口流入 +18%，青云区 75+ 老龄化 +25%，其余 ±3%
                    if region == "丹凤区":
                        base = int(base * 1.18)
                    if region == "青云区" and age == "75+":
                        base = int(base * 1.25)
                    base = int(base * (1 + rng.uniform(-0.03, 0.03)))
                cells.append((region, sex, age, base))
    return cells


def _satisfaction(rng, region, age):
    mean = 3.1
    if region == "丹凤区":
        mean += 0.7      # 丹凤区满意度偏高；样本少 → 不加权会低估总体满意度
    if region == "苍梧区":
        mean += 0.2
    if age == "75+":
        mean -= 0.2
    return min(5, max(1, round(rng.gauss(mean, 0.8))))


def _used_service(rng, region):
    p = 0.42 + (0.1 if region == "青云区" else 0) - (0.08 if region == "丹凤区" else 0)
    return 1 if rng.random() < p else 0


def _make_wave(rng, group_n, respondent_prefix, start_seq):
    """返回 [(respondent_id, region, sex, age_group, satisfaction, used_service), ...]"""
    rows = []
    seq = start_seq
    for (region, sex), total in group_n.items():
        counts = _age_split(rng, total)
        # 特殊构造：苍梧区·男·75+ 只留 2 人（触发年龄合并）
        if (region, sex) == ("苍梧区", "男") and total >= 60:
            counts[4] = 2
            counts[3] += total - sum(counts)
        for age, n in zip(AGES, counts):
            for _ in range(n):
                seq += 1
                rows.append((
                    f"{respondent_prefix}{seq:05d}", region, sex, age,
                    _satisfaction(rng, region, age), _used_service(rng, region),
                ))
    return rows


def wave1_rows():
    rng = random.Random(20260301)
    return _make_wave(rng, _WAVE1_GROUP_N, "W1-", 0)


def batch2_rows():
    rng = random.Random(20260601)
    return _make_wave(rng, _BATCH2_GROUP_N, "B2-", 0)


def batch3_rows():
    rng = random.Random(20260901)
    return _make_wave(rng, _BATCH3_GROUP_N, "B3-", 0)


def benchmark_csv(version):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["region", "sex", "age_group", "population"])
    w.writerows(benchmark_cells(version))
    return out.getvalue()


def samples_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["respondent_id", "region", "sex", "age_group", "satisfaction", "used_service"])
    w.writerows(rows)
    return out.getvalue()
