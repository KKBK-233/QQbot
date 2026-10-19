"""验证人物目录覆盖、账号隔离、翻页与撤回同步，不调用模型或QQ。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from person_index import PersonMemoryIndex


def person_id(platform, user_id):
    return hashlib.md5(f'{platform}_{user_id}'.encode()).hexdigest()


class PersonIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.chat, self.memory, self.cache = [base / x for x in ('chat.db', 'memory.db', 'index.db')]
        with closing(sqlite3.connect(self.chat)) as conn, conn:
            conn.executescript('''
                CREATE TABLE person_info(platform TEXT,user_id TEXT,person_id TEXT,person_name TEXT,user_nickname TEXT);
                CREATE TABLE mai_messages(id INTEGER PRIMARY KEY,message_id TEXT,session_id TEXT,platform TEXT,user_id TEXT,
                    group_id TEXT,user_nickname TEXT,user_cardname TEXT,timestamp REAL,processed_plain_text TEXT,
                    is_picture INTEGER DEFAULT 0,is_emoji INTEGER DEFAULT 0);
            ''')
            conn.execute('INSERT INTO person_info VALUES(?,?,?,?,?)', ('qq','a',person_id('qq','a'),'甲','旧甲'))
            conn.execute('INSERT INTO person_info VALUES(?,?,?,?,?)', ('qq','outside',person_id('qq','outside'),'群外人','群外人'))
        with closing(sqlite3.connect(self.memory)) as conn, conn:
            conn.execute('CREATE TABLE paragraphs(hash TEXT,source TEXT,metadata TEXT,is_deleted INTEGER,expires_at REAL)')
        self.message('a', 'g1', '同名', '甲的新昵称', '甲的原话')
        self.message('b', 'g1', '同名', '乙', '乙的原话')
        self.message('bot', 'g1', '机器人', '', '机器人叙述不作为人物证据')
        self.message('outside', 'g2', '群外人', '', '别的群的发言')
        self.message('a', '', '甲', '', '甲的私聊内容')
        self.message('b', '', '乙', '', '乙的私聊内容')
        self.index = PersonMemoryIndex(self.chat, self.memory, self.cache, person_id,
            lambda platform, uid: uid == 'bot', refresh_seconds=30)

    def message(self, uid, group, nick, card, text):
        with closing(sqlite3.connect(self.chat)) as conn, conn:
            mid = str(conn.execute('SELECT coalesce(max(id),0)+1 FROM mai_messages').fetchone()[0])
            conn.execute('''INSERT INTO mai_messages(message_id,session_id,platform,user_id,group_id,
                user_nickname,user_cardname,timestamp,processed_plain_text) VALUES(?,?,?,?,?,?,?,?,?)''',
                (mid, 's:' + group, 'qq', uid, group, nick, card, float(mid), text))
            return mid

    def paragraph(self, hash_value, *, uid='', session='s:g1', metadata=None, deleted=0, expires=None):
        meta = {'chat_id': session, **(metadata or {})}
        with closing(sqlite3.connect(self.memory)) as conn, conn:
            conn.execute('INSERT INTO paragraphs VALUES(?,?,?,?,?)', (hash_value,
                'person_fact:' + person_id('qq', uid) if uid else 'chat_summary:' + session,
                json.dumps(meta), deleted, expires))

    def listing(self, **kwargs):
        return self.index.list_people(platform='qq', group_id='g1', **kwargs)

    def test_covers_history_without_profile_and_excludes_bot_and_other_group(self):
        result = self.listing()
        self.assertEqual({p['user_id'] for p in result['people']}, {'a', 'b'})
        self.assertEqual(result['total'], 2)
        self.assertTrue(all(p['recent_messages'] for p in result['people']))

    def test_aliases_and_same_nickname_keep_separate_accounts(self):
        self.assertEqual(len(self.listing(query='同名')['people']), 2)
        for name in ('旧甲', '甲的新昵称'):
            self.assertEqual(self.listing(query=name)['people'][0]['user_id'], 'a')
        self.assertEqual(self.listing(query='同名')['total'], 2)

    def test_name_resolution_preserves_account_identity_and_reports_ambiguity(self):
        args = {'platform': 'qq', 'group_id': 'g1'}
        self.assertEqual(self.index.resolve_person('旧甲', **args)['person_id'], person_id('qq', 'a'))
        self.assertEqual(self.index.resolve_person('乙', **args)['person_id'], person_id('qq', 'b'))
        result = self.index.resolve_person('同名', **args)
        self.assertFalse(result['person_id'])
        self.assertEqual(len(result['ambiguous']), 2)
        self.assertFalse(self.index.resolve_person('完全不存在', **args)['ambiguous'])

    def test_pagination_covers_inactive_people_and_new_speakers(self):
        for i in range(130):
            self.message('new' + str(i), 'g1', '新群友' + str(i), '', '只有一次发言')
        first = self.listing(limit=100)
        second = self.listing(limit=100, offset=first['next_offset'])
        self.assertEqual(first['total'], 132)
        self.assertEqual(len(first['people']) + len(second['people']), 132)
        self.assertIsNone(second['next_offset'])
        self.assertEqual(len({p['person_id'] for p in first['people'] + second['people']}), 132)

    def test_private_directory_only_contains_current_user(self):
        result = self.index.list_people(platform='qq', user_id='a')
        self.assertEqual([p['user_id'] for p in result['people']], ['a'])
        self.assertIn('私聊内容', result['people'][0]['recent_messages'][0]['text_excerpt'])
        self.assertEqual(self.index.recent_messages(person_id('qq', 'b'), platform='qq', user_id='a'), [])

    def test_evidence_is_exact_account_and_current_group(self):
        messages = self.index.recent_messages(person_id('qq', 'a'), platform='qq', group_id='g1')
        self.assertEqual([e['text_excerpt'] for e in messages], ['甲的原话'])
        self.assertNotIn('私聊内容', str(messages))
        self.assertEqual(self.index.recent_messages(person_id('qq', 'outside'), platform='qq', group_id='g1'), [])

    def test_deleted_expired_and_unverified_name_mentions_are_not_indexed(self):
        self.paragraph('live', uid='a')
        self.paragraph('deleted', uid='a', deleted=1)
        self.paragraph('expired', uid='a', expires=1)
        self.paragraph('other_group', uid='a', session='s:g2')
        self.paragraph('guessed', metadata={'person_ids': [person_id('qq','b')], 'participants': ['乙']})
        by_id = {p['user_id']: p for p in self.listing()['people']}
        self.assertEqual(by_id['a']['memory_count'], 1)
        self.assertEqual(by_id['b']['memory_count'], 0)

    def test_grounded_summary_resolves_author_in_original_session(self):
        self.paragraph('grounded', metadata={'evidence_policy': 2, 'evidence_message_ids': ['2']})
        self.paragraph('foreign', session='s:g2', metadata={'evidence_policy': 2, 'evidence_message_ids': ['2']})
        self.paragraph('bot_summary', metadata={'evidence_policy': 2, 'evidence_message_ids': ['3']})
        by_id = {p['user_id']: p for p in self.listing()['people']}
        self.assertEqual(by_id['b']['memory_count'], 1)
        self.assertEqual(by_id['a']['memory_count'], 0)

    def test_refresh_adds_people_and_removes_retracted_memory_links(self):
        self.paragraph('live', uid='a')
        self.assertEqual(self.listing(query='甲')['people'][0]['memory_count'], 1)
        self.message('c', 'g1', '丙', '', '新加入历史记录的人')
        with closing(sqlite3.connect(self.memory)) as conn, conn:
            conn.execute('UPDATE paragraphs SET is_deleted=1 WHERE hash=?', ('live',))
        self.index.refresh(force=True)
        self.assertEqual(self.listing()['total'], 3)
        self.assertEqual(self.listing(query='甲')['people'][0]['memory_count'], 0)

    def test_source_databases_remain_unchanged(self):
        before = [p.read_bytes() for p in (self.chat, self.memory)]
        self.listing()
        self.index.refresh(force=True)
        self.assertEqual(before, [p.read_bytes() for p in (self.chat, self.memory)])

    def test_native_iso_timestamps_and_unknown_times_do_not_drop_people(self):
        with closing(sqlite3.connect(self.chat)) as conn, conn:
            conn.execute("UPDATE mai_messages SET timestamp='2026-10-06 12:34:56.000000' WHERE user_id='a'")
            conn.execute("UPDATE mai_messages SET timestamp='unknown' WHERE user_id='b'")
        self.assertEqual(self.listing()['total'], 2)

    def test_media_and_truncated_text_are_marked(self):
        mid = self.message('b', 'g1', '乙', '', '文本' * 200)
        with closing(sqlite3.connect(self.chat)) as conn, conn:
            conn.execute('UPDATE mai_messages SET is_picture=1 WHERE message_id=?', (mid,))
        item = self.listing(query='乙')['people'][0]['recent_messages'][0]
        self.assertTrue(item['contains_media'])
        self.assertTrue(item['truncated'])
        self.assertEqual(len(item['text_excerpt']), 240)

    def test_missing_source_does_not_overwrite_existing_index(self):
        self.listing()
        self.chat.rename(self.chat.with_suffix('.saved'))
        with self.assertRaises(sqlite3.OperationalError):
            self.index.refresh(force=True)
        with closing(sqlite3.connect(self.cache)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM people').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
