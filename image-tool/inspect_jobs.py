"""只读查看最近生图和QQ交付诊断，不生图、不补发、不展示聊天正文。"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys


def collect(root, delivery_dir, limit):
    """合并Windows任务与WSL发送回执，旧任务缺失的字段保留为空。"""
    rows = []
    for path in sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            rid = job['request_id']
            if not isinstance(rid, str) or path.stem != rid:
                continue
            receipt = {}
            if delivery_dir:
                receipt_path = delivery_dir / path.name
                if receipt_path.is_file():
                    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            rows.append({
                'request_id': rid, 'created': job.get('created'), 'state': job.get('state'),
                'queue_seconds': job.get('queue_seconds'), 'backend_seconds': job.get('elapsed_seconds'),
                'max_concurrent': job.get('max_concurrent'),
                'error_code': job.get('error_code'), 'exit_code': job.get('backend_exit_code'),
                'image_exists': (root / (rid + '.png')).is_file(),
                'attempts': job.get('backend_trace', {}).get('attempts'),
                'transport': job.get('backend_transport', 'subprocess' if job.get('backend') == 'codex' else None),
                'requested_background': job.get('requested_background'),
                'requested_image': job.get('backend_trace', {}).get('requested_image'),
                'reported_image': job.get('backend_trace', {}).get('rendered_image'),
                'model_reported': job.get('backend_trace', {}).get('model_reported'),
                'native_route': job.get('backend_trace', {}).get('native_route'),
                'accepted': job.get('backend_trace', {}).get('accepted'),
                'response_status': job.get('backend_trace', {}).get('response_status'),
                'backend_error_code': job.get('backend_trace', {}).get('error_code'),
                'image_call_statuses': job.get('backend_trace', {}).get('image_call_statuses'),
                'image_result_present': job.get('backend_trace', {}).get('image_result_present'),
                'refusal_present': job.get('backend_trace', {}).get('refusal_present'),
                'assistant_text_present': job.get('backend_trace', {}).get('assistant_text_present'),
                'diagnostic_written': job.get('backend_trace', {}).get('diagnostic_written'),
                'delivery_state': receipt.get('state'), 'delivery_seconds': receipt.get('elapsed_seconds'),
            })
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if len(rows) >= limit:
            break
    return rows


def main():
    """管理员只读入口；默认读取生产目录，可用参数检查隔离测试目录。"""
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data/image-tool')
    default_delivery = (Path(r'\\wsl.localhost\Ubuntu\home\qqbot\QQbot\MaiBot\data\plugins\local.qqbot-image\delivery')
                        if os.name == 'nt' else Path('/home/qqbot/QQbot/MaiBot/data/plugins/local.qqbot-image/delivery'))
    parser.add_argument('--delivery-dir', type=Path, default=default_delivery)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--json', action='store_true', help='输出白名单诊断JSON')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('limit须为1到100')
    if not args.data_dir.is_dir():
        parser.error('任务目录不存在')
    if not args.delivery_dir.is_dir():
        print('交付回执目录不可用，QQ发送状态显示为空。')
    rows = collect(args.data_dir, args.delivery_dir, args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            stamp = datetime.fromtimestamp(row['created']).astimezone().isoformat(timespec='seconds') if row['created'] else '?'
            print(f"{stamp} {row['request_id']}\n"
                  f"  后端={row['state']} 耗时={row['backend_seconds']}s 排队={row['queue_seconds']}s "
                  f"提交次数={row['attempts']} 并发上限={row['max_concurrent']} 错误={row['error_code']} "
                  f"QQ={row['delivery_state']} 发送耗时={row['delivery_seconds']}s\n"
                  f"  响应={row['response_status']} 生图终态={row['image_call_statuses']} "
                  f"有图片={row['image_result_present']} 有拒绝={row['refusal_present']} "
                  f"有回答={row['assistant_text_present']} 后端错误={row['backend_error_code']} "
                  f"诊断已保存={row['diagnostic_written']}")


if __name__ == '__main__':
    main()
