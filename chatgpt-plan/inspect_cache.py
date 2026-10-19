"""只读检查实际模型缓存用量；以数据库最新请求为基准，避免服务时钟偏移。"""

from pathlib import Path
from typing import Any, Dict, List

import argparse
import json
import sqlite3


ROOT = Path('/home/qqbot/QQbot/MaiBot')


def summarize(last_id: int = 0) -> List[Dict[str, Any]]:
    with sqlite3.connect('file:' + str(ROOT / 'data/MaiBot.db') + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT model_api_provider_name AS provider, model_name AS model, task_name AS task,
                   COUNT(*) AS requests, SUM(prompt_tokens) AS input_tokens,
                   SUM(prompt_cache_hit_tokens) AS cached_tokens,
                   SUM(prompt_cache_enabled) AS reported_requests,
                   SUM(CASE WHEN prompt_cache_hit_tokens > 0 THEN 1 ELSE 0 END) AS hit_requests,
                   MIN(timestamp) AS first_timestamp, MAX(timestamp) AS last_timestamp
            FROM llm_usage
            WHERE id > ? AND timestamp >= datetime((SELECT MAX(timestamp) FROM llm_usage), '-2 hours')
                  AND request_type NOT LIKE 'plugin.%'
            GROUP BY model_api_provider_name, model_name, task_name ORDER BY requests DESC
        """, (last_id,)).fetchall()
    result = []
    for row in rows:
        entry = dict(row)
        entry['token_hit_rate_percent'] = round(entry['cached_tokens'] / entry['input_tokens'] * 100, 2) if entry['input_tokens'] else None
        result.append(entry)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--after-id', type=int, default=0)
    args = parser.parse_args()
    print(json.dumps({'database_window': summarize(args.after_id)}, ensure_ascii=False, indent=2))
