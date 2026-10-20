"""本机维护 CLI，阶段二不监听网络或接受群聊直接调用。"""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .config import WorkerConfig
from .redaction import Redactor
from .store import exclusive_lock
from .worker import Worker
from .workspace import setup_base

class Parser(argparse.ArgumentParser):
    def error(self,message):
        raise ValueError(message)

def main(argv=None):
    redactor=Redactor.from_env_file()
    parser=Parser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    submit=commands.add_parser('submit')
    submit.add_argument('--requester',required=True)
    submit.add_argument('--task-file',required=True,type=Path)
    submit.add_argument('--base-ref')
    run=commands.add_parser('run')
    mode=run.add_mutually_exclusive_group()
    mode.add_argument('--once',action='store_true')
    mode.add_argument('--loop',action='store_true')
    status=commands.add_parser('status'); status.add_argument('task_id',nargs='?')
    cancel=commands.add_parser('cancel'); cancel.add_argument('task_id')
    setup=commands.add_parser('setup'); setup.add_argument('--full',action='store_true')
    try:
        args=parser.parse_args(argv)
        config=WorkerConfig.from_env()
        redactor=Redactor.from_env_file(config.dsh_home/'.env')
        worker=Worker(config)
        if args.command=='submit':
            raw=args.task_file.read_bytes()
            if len(raw)>65536: raise ValueError('任务文件超过 64 KiB')
            result=worker.submit(args.requester,raw.decode('utf-8-sig'),args.base_ref)
        elif args.command=='status':
            result=worker.store.get(args.task_id) if args.task_id else worker.store.list()
        elif args.command=='cancel': result=worker.store.cancel(args.task_id)
        elif args.command=='setup':
            with exclusive_lock(config.lock_path): result=setup_base(config,args.full)
        else:
            if not config.use_sandbox or sys.platform!='linux':
                raise ValueError('真实执行仅支持 Linux bwrap；无隔离模式仅供离线测试')
            def stop(*_): raise KeyboardInterrupt
            signal.signal(signal.SIGTERM,stop)
            while True:
                result=worker.run_once()
                if not args.loop: break
                if result: print(json.dumps(redactor.value(result),ensure_ascii=False),flush=True)
                time.sleep(2)
        task_failed=args.command=='run' and result is not None and result['status']!='succeeded'
        print(json.dumps(redactor.value({'success':not task_failed,'result':result}),ensure_ascii=False),flush=True)
        return 2 if task_failed else 0
    except KeyboardInterrupt:
        print(json.dumps({'success':False,'error':'worker_stopped'}),file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({'success':False,'error':type(exc).__name__,'detail':redactor.text(str(exc))[:512]},ensure_ascii=False),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
