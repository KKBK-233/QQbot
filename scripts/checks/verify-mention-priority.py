"""离线执行实际宿主调度方法，验证 @ 优先、取消清理与等待回填；不发 QQ 消息。"""
import argparse
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock


class ReqAbortException(Exception):
    pass


def load_parts(path, classes=(), methods=(), functions=()):
    """提取实际源码中的完整方法，避免启动宿主、加载账号或复制实现作为测试。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in classes:
            node.bases = []
            if methods:
                node.body = [n for n in node.body if getattr(n, 'name', '') in methods]
            nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            # 取消辅助函数内部只有异常类型导入；测试用同名异常，不加载完整应用。
            node.body = [n for n in node.body if not isinstance(n, ast.ImportFrom)]
            nodes.append(node)
    env = {'asyncio': asyncio, 'time': time, 'logger': Mock(), 'ReqAbortException': ReqAbortException,
           'global_config': NS(chat=NS(reply_timing=NS(inevitable_at_reply=True, planner_interrupt_max_consecutive_count=0))),
           'focus_mode_manager': NS(can_decide=lambda *a, **kw: True)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), 'exec'), env)
    return env


def setup(root):
    global Runtime, Controller, Scheduler, Engine, await_interrupt
    path = root / 'src/maisaka'
    Controller = load_parts(path / 'runtime.py', classes=['PlannerInterruptController'])['PlannerInterruptController']
    names = ['register_message', '_is_priority_at', '_request_planner_interrupt_for_message',
             '_wait_for_message_quiet_period', '_enter_wait_state', '_schedule_wait_timeout',
             '_cancel_wait_timeout_task', '_schedule_message_turn']
    Runtime = load_parts(path / 'runtime.py', classes=['MaisakaHeartFlowChatting'], methods=names)['MaisakaHeartFlowChatting']
    Scheduler = load_parts(path / 'turn_scheduler.py', classes=['MessageTurnScheduler'],
                           methods=['schedule_message_turn'])['MessageTurnScheduler']
    await_interrupt = load_parts(root / 'src/llm_models/model_client/adapter_base.py',
                                functions=['await_task_with_interrupt'])['await_task_with_interrupt']
    env = load_parts(path / 'reasoning_engine.py', classes=['MaisakaReasoningEngine'], methods=['_run_interruptible_planner'])
    env['await_task_with_interrupt'] = await_interrupt
    Engine = env['MaisakaReasoningEngine']


def runtime(state='wait'):
    r = Runtime()
    r._STATE_WAIT, r._STATE_RUNNING, r._STATE_STOP = 'wait', 'running', 'stop'
    r._agent_state, r._running = state, True
    r.chat_stream, r.log_prefix, r.session_id = NS(is_group_session=True), '[离线测试]', 'test'
    r.message_cache, r._last_processed_index = [], 0
    r._planner_interrupt, r._planner_interrupt_max_consecutive_count = Controller(), 0
    r._wait_timeout_task, r._message_turn_scheduled = None, False
    r._pending_wait_tool_call_id, r._pending_wait_logical_turn_id = 'wait-call', 'turn'
    r._message_debounce_required, r._message_debounce_seconds = False, 1
    r._message_turn_scheduler = Scheduler.__new__(Scheduler)
    r._message_turn_scheduler._runtime = r
    for name in ['_ensure_background_tasks_running', '_record_external_message_interval', '_emit_monitor_message_ingested',
                 '_prune_processed_message_cache', '_cancel_deferred_message_turn_task']:
        setattr(r, name, Mock())
    r._update_message_trigger_state = lambda m: None
    r._is_reply_effect_tracking_enabled = lambda: False
    r._should_continue_after_focus_gate = lambda m: True
    r._clear_message_debounce_required = lambda: setattr(r, '_message_debounce_required', False)
    r._mark_message_debounce_required = lambda: setattr(r, '_message_debounce_required', True)
    r._enter_running_state = lambda: setattr(r, '_agent_state', 'running')
    r._mark_message_turn_unscheduled = lambda: setattr(r, '_message_turn_scheduled', False)
    r._is_reply_frequency_silent = lambda: False
    r._get_pending_message_count = lambda: len(r.message_cache) - r._last_processed_index
    r._get_effective_reply_frequency = lambda: 1
    r._has_forced_turn_trigger = lambda: any(m.is_at for m in r.message_cache[r._last_processed_index:])
    r.enqueued = 0
    def enqueue():
        r.enqueued += 1
        r._message_turn_scheduled = True
    r._enqueue_message_turn = enqueue
    r._idle_backoff = NS(should_delay=lambda count: True)
    return r


def message(at=False):
    return NS(is_at=at, message_id='at' if at else 'ordinary')


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_message_keeps_wait(self):
        r = runtime()
        await r.register_message(message())
        self.assertEqual((r._agent_state, r.enqueued), ('wait', 0))

    async def test_at_cancels_wait_preserves_call_and_deduplicates_turn(self):
        r = runtime()
        timer = asyncio.create_task(asyncio.sleep(120))
        r._wait_timeout_task = timer
        await r.register_message(message(True))
        await r.register_message(message(True))
        await asyncio.sleep(0)
        self.assertTrue(timer.cancelled())
        self.assertEqual((r._agent_state, r.enqueued), ('running', 1))
        self.assertEqual(r._pending_wait_tool_call_id, 'wait-call')
        self.assertEqual(r._pending_wait_logical_turn_id, 'turn')

    async def test_only_at_interrupts_and_repeated_at_is_bounded(self):
        r = runtime('running')
        flag = asyncio.Event()
        r._planner_interrupt.bind(flag)
        await r.register_message(message())
        self.assertFalse(flag.is_set())
        await r.register_message(message(True))
        await r.register_message(message(True))
        self.assertTrue(flag.is_set())
        self.assertEqual(r._planner_interrupt.consecutive_count, 1)
        r._planner_interrupt.unbind(flag, interrupted=True)
        second = asyncio.Event()
        r._planner_interrupt.bind(second)
        r._request_planner_interrupt_for_message(message(True))
        self.assertFalse(second.is_set())

    async def test_at_bypasses_continuous_group_debounce(self):
        r = runtime('running')
        r.message_cache = [message(True)]
        r._message_debounce_required = True
        r._last_message_received_at = time.time()
        await asyncio.wait_for(r._wait_for_message_quiet_period(), .2)
        self.assertFalse(r._message_debounce_required)

    async def test_late_old_wait_cannot_hide_pending_at(self):
        r = runtime('running')
        r.message_cache = [message(True)]
        r._enter_wait_state(120, 'late-wait', 'turn')
        self.assertEqual((r._agent_state, r.enqueued), ('running', 1))
        self.assertIsNone(r._wait_timeout_task)
        self.assertEqual(r._pending_wait_tool_call_id, 'late-wait')
        await asyncio.sleep(0)

    async def test_stalled_planner_cancel_cleans_child_and_unbinds(self):
        r = runtime('running')
        cleaned = asyncio.Event()
        async def stalled(*a, **kw):
            try:
                await asyncio.sleep(120)
            finally:
                cleaned.set()
        r._chat_loop_service = NS(chat_loop_step=stalled, set_interrupt_flag=Mock())
        r._chat_history, r._max_context_size = [], 40
        r._bind_planner_interrupt_flag = r._planner_interrupt.bind
        r._unbind_planner_interrupt_flag = r._planner_interrupt.unbind
        engine = Engine()
        engine._runtime, engine._active_logical_turn_id = r, 'turn'
        task = asyncio.create_task(engine._run_interruptible_planner())
        await asyncio.sleep(.03)
        await r.register_message(message(True))
        with self.assertRaises(ReqAbortException):
            await asyncio.wait_for(task, .5)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(r._planner_interrupt.request(max_consecutive_count=1), 'idle')
        r._chat_loop_service.set_interrupt_flag.assert_called_with(None)

    async def test_normal_planner_result_is_preserved(self):
        r = runtime('running')
        async def finished(*a, **kw):
            return '正常规划结果'
        r._chat_loop_service = NS(chat_loop_step=finished, set_interrupt_flag=Mock())
        r._chat_history, r._max_context_size = [], 40
        r._bind_planner_interrupt_flag = r._planner_interrupt.bind
        r._unbind_planner_interrupt_flag = r._planner_interrupt.unbind
        engine = Engine()
        engine._runtime, engine._active_logical_turn_id = r, 'turn'
        self.assertEqual(await engine._run_interruptible_planner(), '正常规划结果')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='/home/qqbot/QQbot/MaiBot')
    setup(Path(parser.parse_args().folder))
    unittest.main(argv=['verify-mention-priority'], verbosity=2)
