"""weightkit —— 调查样本加权统计系统（虚构数据演示项目）。

三个独立运行的服务共用一个 SQLite 库：
- services/ingest_service.py    样本接收 / 基准发布（只写样本与基准，不算权重）
- services/weighting_service.py 权重计算（可中断、可恢复，产出不可变的权重快照）
- services/report_service.py    报表与下载（只读已发布的快照，保证总表与分组报表同源）
"""

__version__ = "0.1.0"
