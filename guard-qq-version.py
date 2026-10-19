"""保持 SnowLuma 已验证的 QQ 版本，已下载升级文件转移备份，不删除登录态。"""

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shutil

PINNED_VERSION = "3.2.32-52194"
VERSION_DIR = Path("/home/qqbot/.config/QQ/versions")
BACKUP_ROOT = Path("/home/qqbot/QQbot/update-backups")
os.umask(0o077)

# 与上游官方 Docker 一致，只阻止静默升级下载域名。
hosts_path = Path("/etc/hosts")
hosts = hosts_path.read_text()
if not any("qqpatch.gtimg.cn" in line.split("#", 1)[0].split() for line in hosts.splitlines()):
    with hosts_path.open("a") as stream:
        stream.write("\n# QQbot：保留 SnowLuma 兼容客户端版本\n0.0.0.0 qqpatch.gtimg.cn\n")

config_path = VERSION_DIR / "config.json"
if config_path.exists():
    config = json.loads(config_path.read_text())
    if config.get("curVersion") != PINNED_VERSION:
        raise SystemExit("QQ 当前启动版本已改变，停止自动修改，请先检查兼容性。")
    candidates = [path for path in VERSION_DIR.iterdir()
                  if re.fullmatch(r"\d+\.\d+\.\d+-\d+(?:\.zip)*", path.name)
                  and not path.name.startswith(PINNED_VERSION)]
    if candidates or config.get("readyVersion"):
        backup = BACKUP_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, backup / "config.json")
        for path in candidates:
            path.rename(backup / path.name)
        config["readyVersion"] = ""
        config["readyType"] = ""
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        # 使用 qqbot 账号持有备份；维护脚本由 root 调用。
        shutil.chown(BACKUP_ROOT, user="qqbot", group="qqbot")
        for path in [backup, *backup.rglob("*")]:
            shutil.chown(path, user="qqbot", group="qqbot")
        shutil.chown(config_path, user="qqbot", group="qqbot")
        print("已隔离待安装更新包并保留备份，兼容版和 QQ 登录态保持可用。")
