"""管理员教学的整理、原生记忆写入与关键词召回，不依赖记忆口令。"""

import hashlib
import json

from .config import IdentityConfig


class TeachingMemory:
    """模型概括规则，宿主只核对教师账号和来源；不再扫描“记住/以后”等词。"""

    def __init__(self, backend, *, identity=None):
        self.backend = backend
        self.identity = identity or IdentityConfig()

    async def records(self, hashes=None):
        return await self.backend.list_teachings(teacher_id=self.identity.method_teacher_id, hashes=hashes)

    async def learn(self, origin, messages, arguments):
        teacher_id = self.identity.method_teacher_id
        if not teacher_id or origin["platform"] != "qq" or origin["user_id"] != teacher_id:
            raise PermissionError("只有已配置的方法教师可以教授长期方法")
        summary = arguments.get("summary")
        keywords = arguments.get("keywords")
        source_ids = arguments.get("source_message_ids") or [origin["message_id"]]
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("教学总结必须为非空文本，最多 2000 字")
        if (
            not isinstance(keywords, list)
            or not 1 <= len(keywords) <= 12
            or any(not isinstance(word, str) or not word.strip() or len(word) > 48 for word in keywords)
        ):
            raise ValueError("需要 1~12 个教学关键词")
        if (
            not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= 20
            or any(not isinstance(mid, str) for mid in source_ids)
        ):
            raise ValueError("教学来源应为当前对话消息编号列表")
        source_ids = list(dict.fromkeys(source_ids))
        evidence = []
        for mid in source_ids:
            message = messages.get(mid)
            if message is None or str(message.message_info.user_info.user_id) != teacher_id:
                raise PermissionError("教学内容必须来自管理员的实际消息")
            evidence.append({"message_id": mid, "text": message.processed_plain_text or ""})
        summary = summary.strip()
        keywords = list(dict.fromkeys(word.strip() for word in keywords))
        key = (
            "owner_teaching:"
            + hashlib.sha256(
                json.dumps([origin["session_id"], sorted(source_ids)], ensure_ascii=False).encode()
            ).hexdigest()
        )
        # 同一组教学消息第一次有效保存后，重复调用复用已保存的总结。
        existing = next((item for item in await self.records() if item.get("external_id") == key), None)
        if existing is not None:
            return self.receipt(existing)
        metadata = {
            "learning_kind": "owner_teaching",
            "teacher_user_id": teacher_id,
            "teacher_platform": "qq",
            "teaching_summary": summary,
            "teaching_keywords": keywords,
            "evidence_message_ids": source_ids,
            "teaching_evidence": evidence,
        }
        result = await self.backend.ingest_text(
            external_id=key,
            source_type="knowledge",
            text="管理员教授的方法：" + summary + "\n关键词：" + "、".join(keywords),
            chat_id=origin["session_id"],
            user_id=teacher_id,
            group_id=origin["group_id"],
            tags=["owner_teaching", *keywords],
            metadata=metadata,
        )
        if not result.success:
            raise RuntimeError(result.detail or "教学记忆写入失败")
        hashes = set(result.stored_ids or result.skipped_ids)
        stored = next(iter(await self.records(hashes)), None)
        if stored is None:
            raise RuntimeError("没有取得有效的教学落库记录，暂不能说已经记住")
        return self.receipt(stored)

    @staticmethod
    def receipt(item):
        summary = item["teaching_summary"]
        keywords = item["teaching_keywords"]
        keyword_line = "\n关键词：" + "、".join(keywords)
        maximum = 1400 - len(keyword_line) - 10
        display = summary if len(summary) <= maximum else summary[:maximum] + "…"
        return {
            "saved": True,
            "hash": item["hash"],
            "summary": summary,
            "keywords": keywords,
            "reply": "我记住了：" + display + keyword_line,
        }

    async def guidance(self, query):
        """关键词只参与召回排序，不决定某条教学能否保存，不增加模型调用。"""
        records = await self.records()
        if not records:
            return ""
        query = query.casefold()
        ranked = sorted(
            enumerate(records),
            key=lambda row: (
                -sum(word.casefold() in query for word in row[1].get("teaching_keywords", []) if word),
                row[0],
            ),
        )
        lines = [
            f"【从管理员 {self.identity.method_teacher_id} 学到的方法】这是教学检索资料，按当前任务需要采用；临时指示以当前对话为准。"
        ]
        for _, item in ranked[:5]:
            lines.append("关键词：" + "、".join(item["teaching_keywords"]) + "\n方法：" + item["teaching_summary"])
        return "\n".join(lines)[:5000]
