"""让群聊 @ 唤醒固定等待并抢占未执行工具的规划；保留等待回填与原生取消清理。"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil


def replace_once(text, old, new):
    """严格匹配上游代码，重复应用不叠加；上游变化时停止以免误改。"""
    if new in text:
        return text
    if text.count(old) != 1:
        raise ValueError('上游代码与补丁不匹配，未写入')
    return text.replace(old, new, 1)


def patched_sources(root):
    """仅调整 @ 调度和规划请求，工具执行、群聊频率和模型配置保持原设置。"""
    runtime = root / 'src/maisaka/runtime.py'
    text = runtime.read_text(encoding='utf-8')
    text = replace_once(text,
        '        if self._agent_state == self._STATE_RUNNING:\n            self._mark_message_debounce_required()\n        self._request_planner_interrupt_for_message(message)',
        '''        # QQbot：结构化 @ 优先唤醒等待，保留挂起的 wait 调用供原生历史回填。
        if self._is_priority_at(message):
            self._clear_message_debounce_required()
            if self._agent_state == self._STATE_WAIT:
                self._cancel_wait_timeout_task()
                self._enter_running_state()
                logger.info(f"{self.log_prefix} @ 消息提前结束固定等待；消息编号={message.message_id}")
        elif self._agent_state == self._STATE_RUNNING:
            self._mark_message_debounce_required()
        self._request_planner_interrupt_for_message(message)''')
    text = replace_once(text,
        '    def _request_planner_interrupt_for_message(self, message: SessionMessage) -> None:',
        '''    def _is_priority_at(self, message: SessionMessage) -> bool:
        """只让启用必回的群聊结构化 @ 获得抢占优先级。"""
        return bool(self.chat_stream.is_group_session and message.is_at
                    and global_config.chat.reply_timing.inevitable_at_reply)

    def _request_planner_interrupt_for_message(self, message: SessionMessage) -> None:''')
    text = replace_once(text,
        '        planner_interrupt_max_count = self._planner_interrupt_max_consecutive_count\n        request_result = self._planner_interrupt.request(max_consecutive_count=planner_interrupt_max_count)',
        '''        # 普通群消息不抢占；@ 至少允许一次原生规划中断，连续刷 @ 仍受上限约束。
        if self.chat_stream.is_group_session and not self._is_priority_at(message):
            return
        planner_interrupt_max_count = self._planner_interrupt_max_consecutive_count
        if self._is_priority_at(message):
            planner_interrupt_max_count = max(1, planner_interrupt_max_count)
        request_result = self._planner_interrupt.request(max_consecutive_count=planner_interrupt_max_count)''')
    text = replace_once(text,
        '        while self._running:\n            elapsed = time.time() - self._last_message_received_at',
        '''        while self._running:
            # 群聊再热闹也不能用静默窗口拖住待处理的 @。
            if any(self._is_priority_at(m) for m in self.message_cache[self._last_processed_index:]):
                break
            elapsed = time.time() - self._last_message_received_at''')
    text = replace_once(text,
        '                self._schedule_wait_timeout(seconds=seconds, tool_call_id=tool_call_id)\n            )',
        '''                self._schedule_wait_timeout(seconds=seconds, tool_call_id=tool_call_id)
            )
        # 防止 @ 刚好到达规划收尾时，旧规划随后用 wait 再次压住新消息。
        if any(self._is_priority_at(m) for m in self.message_cache[self._last_processed_index:]):
            self._cancel_wait_timeout_task()
            self._enter_running_state()
            self._schedule_message_turn()''')
    engine = root / 'src/maisaka/reasoning_engine.py'
    value = engine.read_text(encoding='utf-8')
    value = replace_once(value,
        'from src.llm_models.exceptions import ReqAbortException, RespNotOkException',
        'from src.llm_models.exceptions import ReqAbortException, RespNotOkException\nfrom src.llm_models.model_client.adapter_base import await_task_with_interrupt')
    value = replace_once(value,
        '''            return await self._runtime._chat_loop_service.chat_loop_step(
                self._runtime._chat_history,
                injected_user_messages=injected_user_messages,
                tail_user_messages=tail_user_messages,
                tool_definitions=tool_definitions,
                max_context_size=self._runtime._max_context_size,
                logical_turn_id=self._active_logical_turn_id,
            )''',
        '''            # 整个规划等待都监测中断，流式请求暂时没有新数据时也能取消并清理。
            planner_task = asyncio.create_task(self._runtime._chat_loop_service.chat_loop_step(
                self._runtime._chat_history,
                injected_user_messages=injected_user_messages,
                tail_user_messages=tail_user_messages,
                tool_definitions=tool_definitions,
                max_context_size=self._runtime._max_context_size,
                logical_turn_id=self._active_logical_turn_id,
            ))
            return await await_task_with_interrupt(planner_task, interrupt_flag)''')
    wait = root / 'src/maisaka/builtin_tool/wait.py'
    description = wait.read_text(encoding='utf-8').replace(
        '期间收到的新消息不会提前打断本次等待。', '普通群消息不会提前打断；群聊 @ 会提前结束等待。')
    results = {runtime: text, engine: value, wait: description}
    for path, content in results.items():
        compile(content, str(path), 'exec')
    return results


def repair(root):
    sources = patched_sources(root)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for path, content in sources.items():
        if path.read_text(encoding='utf-8') != content:
            backup = root / 'run/config-backups' / stamp / path.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
            path.write_text(content, encoding='utf-8')
    print('群聊 @ 优先调度补丁已应用，源码校验通过；未重启 QQ 或调用模型')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='/home/qqbot/QQbot/MaiBot')
    repair(Path(parser.parse_args().folder))
