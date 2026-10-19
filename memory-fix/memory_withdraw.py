"""撤回旧段落及其人物事实账本，避免只删正文却仍从画像里返回旧事实。"""
import sqlite3
from src.A_memorix.host_service import a_memorix_host_service
from src.services.memory_service import memory_service


async def withdraw_paragraph(hash_value, requested_by, reason):
    """通过原生管理接口记录撤回审计，之后可从原生回收站和数据库备份恢复。"""
    path = a_memorix_host_service.get_runtime_data_dir() / 'metadata/metadata.db'
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as conn:
        claims = conn.execute("SELECT DISTINCT c.claim_id FROM fact_claims c JOIN fact_evidence e ON e.claim_id=c.claim_id "
                              "WHERE e.evidence_type='paragraph' AND e.evidence_id=? AND c.status='active'", (hash_value,)).fetchall()
    for (claim_id,) in claims:
        result = await memory_service.fact_admin(action='retract', claim_id=claim_id,
                                                 requested_by=requested_by, reason=reason)
        if not result.get('success'):
            return {'success': False, 'error': '旧画像事实撤回失败'}
    return await memory_service.delete_admin(action='execute', mode='paragraph', selector={'hashes': [hash_value]},
                                             requested_by=requested_by, reason=reason)
