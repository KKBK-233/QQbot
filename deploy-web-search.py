"""部署独立搜索插件，保留运行配置与插件自己的 Git 历史。"""

from pathlib import Path
import shutil

source = Path('/mnt/d/code/web/QQbot/MaiBot/plugins/qqbot-web-search')
target = Path('/home/qqbot/QQbot/MaiBot/plugins/qqbot-web-search')
target.mkdir(exist_ok=True)
# 只复制版本控制文件，不复制 Git、缓存或覆盖现有运行配置。
for name in ['_manifest.json', 'plugin.py', 'search_backend.py', 'google_worker.py', 'reply_bridge.py', 'README.md', 'test_search.py', 'test_reply_bridge.py', 'webpage_reader.py', 'webpage_worker.py', 'test_webpage.py', 'browser_reader.py', 'browser_worker.py', 'test_browser.py', 'manual_article.py', 'test_manual_article.py']:
    shutil.copy2(source / name, target / name)
    assert (source / name).read_bytes() == (target / name).read_bytes()
print('搜索插件部署完成，文件内容已核对。')
