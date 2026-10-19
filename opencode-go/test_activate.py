"""验证全量路由切换不会启用空任务或混入不兼容的模型能力。"""
from copy import deepcopy
import unittest

from activate import task_routes


class TaskRoutesTests(unittest.TestCase):
    def setUp(self):
        self.config = {'model_task_config': {
            name: {'model_list': ['old'], 'max_tokens': 8192}
            for name in ('planner', 'replyer', 'memory', 'mid_memory', 'utils',
                         'learner', 'expression_use', 'emoji', 'vlm')}}
        for name in ('voice', 'embedding', 'image_embedding'):
            self.config['model_task_config'][name] = {'model_list': []}

    def test_all_enabled_tasks_including_vision(self):
        before = deepcopy(self.config)
        routes = task_routes(self.config, 'haiku', all_tasks=True)
        self.assertEqual(len(routes), 9)
        self.assertEqual(set(routes.values()), {'haiku'})
        self.assertIn('vlm', routes)
        self.assertEqual(self.config, before)

    def test_default_still_only_main_conversation(self):
        self.assertEqual(task_routes(self.config, 'haiku'), {'planner': 'haiku', 'replyer': 'haiku'})

    def test_enabled_embedding_is_rejected(self):
        self.config['model_task_config']['embedding']['model_list'] = ['embedding-model']
        with self.assertRaises(ValueError):
            task_routes(self.config, 'haiku', all_tasks=True)

    def test_full_rollback_restores_original_models_and_keeps_disabled_tasks(self):
        before = deepcopy(self.config)
        routes = task_routes(self.config, 'haiku', rollback=True, all_tasks=True)
        self.assertEqual(len(routes), 9)
        self.assertEqual(routes['planner'], 'deepseek-v4-flash-thinking')
        self.assertEqual(routes['replyer'], 'deepseek-v4-flash')
        self.assertEqual(routes['vlm'], 'deepseek-flash')
        self.assertEqual(self.config, before)
        self.assertNotIn('embedding', routes)


if __name__ == '__main__':
    unittest.main()
