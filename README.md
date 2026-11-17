# weightkit —— 调查样本加权统计系统（虚构数据演示）

从空目录实现的可运行项目：调查样本持续补报、人口基准换版场景下的
**样本接收 / 权重计算 / 报表** 三服务架构。所有数据为虚构（云澜市社区服务
满意度调查），所有统计结论只针对给定样本设计，不对总体代表性作额外主张。

## 快速开始

```bash
python3 scripts/run_all.py     # 启动三个服务（Ctrl-C 停止）
python3 scripts/demo.py        # 端到端演示：基准→补报→重算→中断恢复→修订比较
# 打开 http://localhost:8003/ 使用 Web 界面
python3 -m unittest discover -s tests   # 27 个测试
```

也可以三个终端各自启动（生产形态，互不影响）：

```bash
python3 services/ingest_service.py      # :8001 样本接收
python3 services/weighting_service.py   # :8002 权重计算
python3 services/report_service.py      # :8003 报表 + Web UI
```

纯标准库实现（Python 3.8+），无第三方依赖；存储为 SQLite（WAL），
库文件默认在 `data/weightkit.db`（可用 `WEIGHTKIT_DB` 覆盖）。

## 架构与一致性保证

```
                ┌──────────────────────┐
 基准CSV ─────► │ 样本接收服务 :8001     │  只校验入库，绝不算权重
 补报CSV ─────► │ (ingest_service)     │
                └─────────┬────────────┘
                          │ 共享 SQLite (WAL)
                ┌─────────▼────────────┐
                │ 权重计算服务 :8002     │  全量/增量重算，可中断恢复
                │ (weighting_service)  │  产出不可变快照 → 发布
                └─────────┬────────────┘
                          │ current_snapshot 单行指针（原子切换）
                ┌─────────▼────────────┐
                │ 报表服务 :8003         │  只读已发布快照
                │ (report_service)     │  总表/分组/比较/下载/Web UI
                └──────────────────────┘
```

**为什么不会出现"总表已更新、分组报表还用旧权重"：**
每次计算产生一个不可变的 `weight_runs` 快照（记录级权重、估计单元、
无法估计层都挂在 run_id 下）。发布 = 在一个事务里替换 `current_snapshot`
单行指针。报表服务的总表、分组报表、无法估计清单全部从该指针指向的
同一个 run 计算，且每个响应都带 `snapshot_id` —— 不存在部分更新的窗口。

**每条记录的权重依据**（`record_weights` 表，可下载 CSV）：
`run_id`、基准版本、最终估计单元 `cell_key`、合并级别 `merge_level`、
单元样本量 `n_cell`、单元基准人口 `n_pop`、原始权重 `raw_weight`、
是否修剪 `trimmed`、最终权重 `weight`、计算时间 `computed_at`。

## 加权方法（事后分层）

`weight = 校准因子 × 截断( n_pop / n_cell )`

1. **层合并阶梯**（不跨区县，确定性算法）：
   - L0：原始层（区县×性别×年龄段），样本量 ≥ `min_cell_size`（默认 5）直接估计；
   - L1：同（区县， 性别）内按年龄顺序贪心合并相邻层，直到每个单元达标；
   - L2：仅当某区县**所有**性别组各自不达标、但合并后达标时，合并性别；
   - 若部分性别组充足、部分不足：充足组正常估计，不足组**判为无法估计**
     （不为凑数把充足组的估计质量拉低）；
   - 整个区县样本仍不足 → 无法估计。
2. **空层 / 无法估计层**：人口不计入估计，计入"未覆盖人口"，
   在报表和 `stratum_weights.csv` 中逐层列出并注明原因
   （样本为空、基准外样本、基准人口为 0 等）。
3. **极端权重**：按记录级原始权重的分位数（默认 2%/98%）截断并打标
   `trimmed=1`，再整体校准使权重和 = 覆盖人口。

## 全量重算 vs 只修正受影响层（取舍）

| | 全量重算 `full` | 增量重算 `affected` |
|---|---|---|
| 范围 | 所有层 | 受影响层**所在区县**（见下） |
| 修剪边界 | 用最新数据重算分位 | **冻结**为父运行的边界 |
| 校准 | 全局重算 | 只对受影响部分做残差校准，未受影响记录**逐字节不变** |
| 全局参数时效 | 始终最新 | 随数据漂移逐渐过时 |
| 成本 | O（全部记录） | O（受影响区县） |

- 受影响层 = 父运行之后新到批次涉及的层 ∪ 基准换版中人口变化的层。
- 由于合并阶梯不跨区县，受影响集合的闭包就是"受影响层所在的区县"，
  因此增量结果可以与全量结果对照验证（测试
  `test_affected_equals_full_without_trimming`：不修剪时两者逐记录一致）。
- 修剪分位是全局量，增量模式若重算就会改变未受影响记录的权重，
  违背"只修正受影响层"的承诺 —— 所以选择冻结，并在 `stats.incremental_since_full`
  中记录距上次全量重算的增量次数，提示定期全量重算。
- 校准因子异常（≤0 或 >10）时运行会带警告，建议全量重算。

## 中断与恢复

- 计划阶段（层划分、修剪边界、校准因子）一个事务落库；
- 记录级权重按 chunk 写入，每 chunk 一个事务并推进 `checkpoint`；
- `POST /api/runs/{id}/interrupt` 置标志位，工作线程在 chunk 边界停为
  `interrupted`；`POST .../resume` 从 checkpoint 继续；
- 进程崩溃/重启后，遗留的 `running` 自动转为 `interrupted`，可恢复；
- 测试 `test_interrupt_resume_equals_uninterrupted` 验证：
  中断再恢复的最终权重与不中断完全一致。

## API 摘要

**样本接收 :8001**
- `POST /api/benchmarks?name=&note=`（body: CSV `region,sex,age_group,population`）发布基准版本
- `POST /api/samples?source=&note=`（body: CSV `respondent_id,region,sex,age_group,satisfaction,used_service`）接收补报批次
- `GET /api/benchmarks`、`GET /api/batches`

**权重计算 :8002**
- `POST /api/runs` `{mode: full|affected, benchmark_version_id, config?}`
- `GET /api/runs`、`GET /api/runs/{id}`（进度、受影响层、警告）
- `POST /api/runs/{id}/interrupt|resume|publish`

**报表 :8003**
- `GET /api/reports/master`、`GET /api/reports/groups?dim=region|sex|age_group`
- `GET /api/reports/unestimable`
- `GET /api/compare?from=<run>&to=<run>`（修订影响：估计变化、权重变化记录数、
  新可估计/新无法估计层、未覆盖人口变化）
- `GET /api/download/record_weights.csv`、`GET /api/download/stratum_weights.csv`
  （`?run_id=` 可选，默认当前发布快照）

## 诚实性声明

- 数据由 `weightkit/seed.py` 确定性生成，**完全虚构**；
- 加权是给定样本设计下的事后分层估计，结论只适用于该样本设计；
- 无法估计层的人口不进入任何估计，单独报告为未覆盖人口；
- 本系统不对总体代表性作超出样本设计的主张，报表页与 API 响应
  均携带该声明（`honesty_note`）。

## 目录

```
weightkit/            核心库：db / ingest_lib / weighting / reports / seed / httpserver
services/             三个独立服务（薄 HTTP 壳）
web/                  Web UI（无框架，直连三个服务）
scripts/run_all.py    一键启动；scripts/demo.py 端到端演示
tests/                27 个单元/集成测试
data/                 运行期 SQLite 与导出（git 忽略）
```
