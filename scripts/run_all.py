#!/usr/bin/env python3
"""一键启动三个独立服务（开发/演示用）。

生产形态是三个进程各自启动：
    python3 services/ingest_service.py     # :8001
    python3 services/weighting_service.py  # :8002
    python3 services/report_service.py     # :8003  (Web UI: http://localhost:8003)
本脚本只是把三者拉起来并转发 Ctrl-C。
"""

import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = ["ingest_service.py", "weighting_service.py", "report_service.py"]


def main():
    procs = []
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    for svc in SERVICES:
        path = os.path.join(ROOT, "services", svc)
        procs.append(subprocess.Popen([sys.executable, path], env=env))
        time.sleep(0.3)
    print("三个服务已启动：")
    print("  样本接收  http://localhost:8001/health")
    print("  权重计算  http://localhost:8002/health")
    print("  报表+UI   http://localhost:8003/")
    print("Ctrl-C 全部停止。")

    def shutdown(*_):
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while True:
        for p in procs:
            if p.poll() is not None:
                print(f"服务进程退出（code={p.returncode}），全部停止。")
                shutdown()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
