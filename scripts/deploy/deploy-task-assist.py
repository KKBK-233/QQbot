"""部署独立任务插件，逐文件核对，不复制运行数据或凭据。"""

from pathlib import Path
import shutil
import json

source = Path('/mnt/d/code/web/QQbot/MaiBot/plugins/qqbot-task-assist')
target = Path('/home/qqbot/QQbot/MaiBot/plugins/qqbot-task-assist')
# 先修正本地部署源，后续重部署也保留当前图片绑定。
import importlib.util
spec = importlib.util.spec_from_file_location('visual_target_repair', Path(__file__).with_name('repair-visual-target.py'))
visual_repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual_repair)
visual_repair.apply(source)

# 两种工具共用媒体读取和 QQ 补取；仅增加查询真实群会话所需的只读能力。
helper_folder = Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin')
for name in ('message_media.py', 'qq_media.py', 'forward_vision.py'):
    shutil.copy2(helper_folder / name, source / name)
manifest_path = source / '_manifest.json'
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
if 'chat.get_group_streams' not in manifest['capabilities']:
    manifest['capabilities'].append('chat.get_group_streams')
if manifest['version'] == '1.0.4':
    manifest['version'] = '1.0.5'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
target.mkdir(exist_ok=True)
for name in ['_manifest.json', 'plugin.py', 'task_flow.py', 'visual_target.py', 'message_media.py', 'qq_media.py', 'forward_vision.py', 'test_task_flow.py', 'README.md']:
    shutil.copy2(source / name, target / name)
    assert (source / name).read_bytes() == (target / name).read_bytes()
print('任务与引用识图插件部署完成，文件逐项一致。')
# 清除旧部署遗留的语气 Hook；保持助手正文、工具和原生思考配套完整。
import importlib.util
spec = importlib.util.spec_from_file_location('remove_tone_filter', Path(__file__).with_name('remove-tone-filter.py'))
tone_cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tone_cleanup)
tone_cleanup.apply(target.parent.parent)
# 已启用思考时，重部署语气插件仍保留连续工具调用必需的原生推理字段。
if (target.parent.parent / 'deepseek_thinking_compat.py').exists():
    import importlib.util
    spec = importlib.util.spec_from_file_location('thinking_deploy', Path(__file__).with_name('enable-planner-thinking.py'))
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    support.install_compat(target.parent.parent)
