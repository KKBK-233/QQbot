"""管理员只读查看滚动24小时预算和实际执行用量，不显示代码或身份。"""

from contextlib import closing
import json
import sqlite3
import time

from budget import STATE, CPU_RESERVE_MS


def main():
    """资源单位单独列出，不把本地计算量伪装成供应商账单或模型token。"""
    path = STATE / 'usage.sqlite3'
    if not path.exists():
        print(json.dumps({'admitted_runs': 0, 'note': '尚无沙箱执行记录'}, ensure_ascii=False))
        return
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT status,cpu_ms,wall_ms,memory_peak_bytes,output_bytes FROM jobs WHERE started>?',
                          (time.time() - 86400,)).fetchall()
    print(json.dumps({'window': 'rolling_24h', 'admitted_runs': len(rows), 'limit_runs': 100,
                      'reserved_cpu_seconds': len(rows) * CPU_RESERVE_MS / 1000,
                      'reservation_limit_cpu_seconds': 400,
                      'measured_cpu_seconds': round(sum(r[1] for r in rows) / 1000, 3),
                      'measured_wall_seconds': round(sum(r[2] for r in rows) / 1000, 3),
                      'sampled_peak_memory_mib': round(max([r[3] for r in rows] or [0]) / 1048576, 2),
                      'model_tool_result_bytes': sum(r[4] for r in rows),
                      'ok': sum(r[0] == 'ok' for r in rows), 'failed': sum(r[0] == 'failed' for r in rows),
                      'pending_or_interrupted': sum(r[0] == 'pending' for r in rows),
                      'note': 'CPU为cgroup实测；内存峰值为20ms采样。中断记录可能缺少实测值但不退配额。模型token和电费不在此账本。'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
