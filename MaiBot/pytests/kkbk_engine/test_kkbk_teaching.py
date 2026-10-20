"""主动教学回归：无口令、网址示例、落库回执、身份限定、删除与关键词召回。"""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import hashlib
import json
import sqlite3
import time
import pytest

from src.kkbk_engine.teaching import TeachingMemory,read_teachings


class Backend:
    def __init__(self,path):
        self.path=path; self.calls=[]; self.fail=False
    async def ingest_text(self,**arguments):
        self.calls.append(arguments)
        if self.fail: return SimpleNamespace(success=False,detail='fixture write failed')
        h=hashlib.sha256(arguments['text'].encode()).hexdigest()
        metadata={**arguments['metadata'],'external_id':arguments['external_id']}
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO paragraphs VALUES(?,?,?,0,NULL)',(h,json.dumps(metadata),time.time()))
        return SimpleNamespace(success=True,stored_ids=[h],skipped_ids=[],detail='')


@pytest.fixture
def learning(tmp_path):
    path=tmp_path/'metadata.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE paragraphs(hash TEXT,metadata TEXT,created_at REAL,is_deleted INT,expires_at REAL)')
    backend=Backend(path)
    memory=TeachingMemory(backend,path)
    origin={'platform':'qq','user_id':'1020050112','session_id':'session','group_id':'1022382380','message_id':'m1'}
    message=SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id='1020050112')),
                            processed_plain_text='比如看 https://example.invalid/api 再和 GitHub 的实现对照，代码示例也要核实。')
    args={'summary':'搜索引擎与 GitHub 分别检索并交叉核实原文，保留来源链接。',
          'keywords':['搜索','GitHub','检索'],'source_message_ids':['m1']}
    return memory,backend,origin,{'m1':message},args


def test_teaching_without_memory_command_and_with_url_is_saved(learning):
    memory,backend,origin,messages,args=learning
    result=asyncio.run(memory.learn(origin,messages,args))
    assert result['saved'] and result['reply'].startswith('我记住了：')
    assert '关键词：搜索、GitHub、检索' in result['reply']
    assert backend.calls[0]['source_type']=='knowledge'
    assert 'https://' in backend.calls[0]['metadata']['teaching_evidence'][0]['text']
    assert args['summary'] in memory.guidance('GitHub 怎么检索')


def test_only_owner_can_teach_even_when_another_user_says_remember(learning):
    memory,backend,origin,messages,args=learning
    with pytest.raises(PermissionError):
        asyncio.run(memory.learn({**origin,'user_id':'stranger'},messages,args))
    messages['m1'].message_info.user_info.user_id='stranger'
    with pytest.raises(PermissionError): asyncio.run(memory.learn(origin,messages,args))
    assert not backend.calls


def test_repeat_returns_original_saved_summary_and_keywords(learning):
    memory,backend,origin,messages,args=learning
    first=asyncio.run(memory.learn(origin,messages,args))
    second=asyncio.run(memory.learn(origin,messages,{**args,'summary':'模型补试时改写了总结'}))
    assert first==second and len(backend.calls)==1


def test_failed_or_filtered_write_never_claims_memory_success(learning):
    memory,backend,origin,messages,args=learning
    backend.fail=True
    with pytest.raises(RuntimeError,match='fixture write failed'):
        asyncio.run(memory.learn(origin,messages,args))
    async def filtered(**_):
        return SimpleNamespace(success=True,stored_ids=[],skipped_ids=['external-only'],detail='chat_filtered')
    backend.ingest_text=filtered
    with pytest.raises(RuntimeError,match='落库记录'): asyncio.run(memory.learn(origin,messages,args))


def test_deleted_and_expired_teaching_is_not_recalled(learning):
    memory,backend,origin,messages,args=learning
    result=asyncio.run(memory.learn(origin,messages,args))
    with sqlite3.connect(backend.path) as db:
        db.execute('UPDATE paragraphs SET expires_at=?',(time.time()-1,))
    assert not memory.guidance('GitHub')
    with sqlite3.connect(backend.path) as db:
        db.execute('UPDATE paragraphs SET expires_at=NULL,is_deleted=1')
    assert read_teachings(backend.path)==[]


def test_long_receipt_stays_within_reply_budget_and_retains_keywords(learning):
    memory,backend,origin,messages,args=learning
    args={**args,'summary':'详细的步骤。'*330,'keywords':[str(i)+'检索'*20 for i in range(12)]}
    result=asyncio.run(memory.learn(origin,messages,args))
    assert len(result['reply'])<=1400
    assert result['keywords'][-1] in result['reply']
