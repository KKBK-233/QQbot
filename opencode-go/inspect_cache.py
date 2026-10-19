"""只读检查OpenCode缓存，按任务区分生产请求与隔离探针。"""
import argparse
import json
from pathlib import Path
import sqlite3

ROOT = Path('/home/qqbot/QQbot/MaiBot')


def usage_summary(rows):
    """缺少计数时保留未知状态，分母包括新输入、缓存写入和读取。"""
    known = [row for row in rows if row.get('total_input_tokens') is not None]
    total = sum(row['total_input_tokens'] for row in known)
    read = sum(row.get('cache_read_input_tokens') or 0 for row in known)
    return {'requests':len(rows),'reported_requests':len(known),'input_tokens':total,
            'cache_read_tokens':read,'cache_write_tokens':sum(row.get('cache_creation_input_tokens') or 0 for row in known),
            'uncached_tokens':sum(row.get('input_tokens') or 0 for row in known),
            'token_hit_rate_percent':round(read / total * 100,2) if total else None}


def inspect(after_id=0, include_tests=False, include_probe=False):
    """以请求ID作上线基线；只读数据库，不打印提示词、消息或令牌。"""
    filter_clause = '' if include_tests else " AND request_type NOT LIKE 'plugin.%' AND request_type NOT LIKE 'manual.%'"
    with sqlite3.connect('file:' + str(ROOT / 'data/MaiBot.db') + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('''
            SELECT model_api_provider_name AS provider, model_name AS model, task_name AS task,
                   COUNT(*) AS requests, SUM(prompt_tokens) AS input_tokens,
                   SUM(prompt_cache_hit_tokens) AS cached_tokens,
                   SUM(prompt_cache_enabled) AS reported_requests,
                   SUM(CASE WHEN prompt_cache_hit_tokens > 0 THEN 1 ELSE 0 END) AS hit_requests,
                   MIN(id) AS first_id, MAX(id) AS last_id
            FROM llm_usage
            WHERE (model_api_provider_name = 'OpenCodeGo' OR model_name LIKE '%haiku%') AND id > ?
        ''' + filter_clause + ' GROUP BY model_api_provider_name, model_name, task_name', (after_id,)).fetchall()
    production = []
    for row in rows:
        item = dict(row)
        item['token_hit_rate_percent'] = round(item['cached_tokens'] / item['input_tokens'] * 100, 2) if item['input_tokens'] else None
        item['request_hit_rate_percent'] = round(item['hit_requests'] / item['requests'] * 100, 2) if item['requests'] else None
        production.append(item)
    telemetry_path = ROOT / 'data/opencode-go/cache-usage.jsonl'
    telemetry=[]
    if telemetry_path.exists():
        for line in telemetry_path.read_text().splitlines():
            try:
                row=json.loads(line)
            except ValueError:
                continue
            if isinstance(row,dict) and row.get('task') in {'planner','replyer'}:
                telemetry.append(row)
    result={'after_id':after_id,'isolated_requests_included':include_tests,'production_usage':production,
            'native_telemetry_by_task':{task:usage_summary([row for row in telemetry if row['task']==task]) for task in ['planner','replyer']}}
    if include_probe:
        reports = sorted((ROOT / 'data/opencode-go').glob('cache-probe-*.json'), key=lambda p:p.stat().st_mtime)
        result['latest_isolated_probe']=json.loads(reports[-1].read_text()) if reports else None
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--after-id',type=int,default=0)
    parser.add_argument('--include-tests',action='store_true')
    parser.add_argument('--include-probe',action='store_true')
    args=parser.parse_args()
    print(json.dumps(inspect(args.after_id,args.include_tests,args.include_probe),ensure_ascii=False,indent=2))
