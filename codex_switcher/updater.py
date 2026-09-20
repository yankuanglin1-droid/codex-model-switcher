"""把自己换成新版本。

界面上的「检查更新」只回答"有没有新版"，这一层负责**真的换掉自己**。

为什么必须绕一圈：要替换的 .app 正是当前进程所在的那一个。进程还在跑的
时候直接删自己的包体，行为取决于 macOS 的心情，不是能依赖的东西。所以流程是：

    下载并校验 → 写一个独立的更新脚本 → 把它脱离本进程启动 → 本进程退出
    → 脚本等我们死透 → 备份旧包 → 换上新包 → 重新打开

更新脚本必须用 `ditto` 解 zip，不能用 Python 的 zipfile：后者不还原 Unix
权限位，launch.sh 解压出来会变成不可执行，结果"更新成功"却再也打不开。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import PROJECT_URL, __version__, paths

APP_NAME = "ChatGPT Model Switcher"
# 应用改过名（旧名：codex（ChatGPT App）多平台模型切换）。从旧版升级时
# 跑的还是旧名的 App，两个名字都得退出，否则旧窗口杀不掉、包体换不干净。
LEGACY_APP_NAMES = ("codex（ChatGPT App）多平台模型切换",)
STANDARD_ASSET = "Codex-Model-Switcher-macOS-latest.zip"
FULL_ASSET = "Codex-Model-Switcher-macOS-latest-full.zip"
DOWNLOAD_TIMEOUT = 180
QUIT_WAIT_SECONDS = 45


class UpdateError(Exception):
    """更新失败。消息会直接显示给用户，所以要写成能看懂的话。"""


def _version_of(app: Path) -> Optional[str]:
    """从 .app 里读出版本号。读不到返回 None（让调用方决定要不要拒绝）。"""
    init = app / "Contents" / "Resources" / "runtime" / "codex_switcher" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return match.group(1) if match else None


def _numbers(text: str) -> Tuple:
    found = re.findall(r"\d+", (text or "").lstrip("vV"))
    return tuple(int(item) for item in found[:3]) or (0,)


def running_app_path() -> Optional[Path]:
    """找出当前进程所在的 .app 包体。

    从本模块的路径往上找第一个以 `.app` 结尾的目录。找不到就说明不是以
    .app 形式运行的（比如 clone 仓库后直接 `python -m` 跑），那种情况下
    本模块无从下手 —— 返回 None，让上层给出"请手动下载"的提示。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_variant(app: Path) -> str:
    """判断装的是标准版还是完整版。

    完整版在 runtime 里自带 Python（launch.sh 就是这么找的）。判断方式必须
    和 launch.sh 保持一致，否则会下错包：完整版用户被换成标准版，机器上
    又没有系统 Python，更新完直接打不开。
    """
    runtime = app / "Contents" / "Resources" / "runtime"
    for candidate in ("python", "python-arm64", "python-x86_64"):
        if (runtime / candidate / "bin" / "python3").exists():
            return "full"
    return "standard"


def asset_url(variant: str) -> str:
    name = FULL_ASSET if variant == "full" else STANDARD_ASSET
    return "%s/releases/latest/download/%s" % (PROJECT_URL, name)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={
        "User-Agent": "codex-model-switcher/" + __version__,
    })
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError("下载失败（HTTP %s）。可以稍后重试，或到 GitHub 手动下载。" % exc.code)
    except Exception as exc:  # noqa: BLE001 - 网络错误种类太多，统一给一句人话
        raise UpdateError("下载失败（%s）。请检查网络后重试。" % type(exc).__name__)
    if len(payload) < 1024:
        raise UpdateError("下载到的文件太小（%d 字节），不像一个安装包，已放弃。" % len(payload))
    destination.write_bytes(payload)


def _extract(zip_path: Path, workdir: Path) -> Path:
    """解压并返回里面的 .app 路径。

    刻意用 ditto 而不是 Python 的 zipfile：zipfile 不还原 Unix 权限位，
    launch.sh 会变成不可执行，App 更新完就再也打不开了。
    """
    if shutil.which("ditto"):
        result = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(workdir)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError("解压失败：%s" % (result.stderr or "").strip())
    elif shutil.which("unzip"):
        result = subprocess.run(["unzip", "-q", "-o", str(zip_path), "-d", str(workdir)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError("解压失败：%s" % (result.stderr or "").strip())
        _restore_exec_bits(workdir)
    else:
        raise UpdateError("本机没有 ditto 或 unzip，无法解压安装包。")
    found = sorted(workdir.glob("*.app"))
    if not found:
        raise UpdateError("安装包里没有找到 .app，已放弃更新。")
    return found[0]


def _restore_exec_bits(root: Path) -> None:
    """unzip 兜底路径：把脚本的执行位补回来。"""
    for script in root.rglob("*.sh"):
        try:
            script.chmod(0o755)
        except OSError:
            pass


def _writable(directory: Path) -> bool:
    try:
        probe = directory / ".switcher-update-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def update_dir() -> Path:
    return paths.state_dir() / "update"


def _write_script(old_app: Path, new_app: Path, backup: Path,
                  wait_pids: list, workdir: Path) -> Path:
    """生成那个「等我们死透再动手」的更新脚本。

    用模板而不是一串字符串相加：拼接写法漏一个 `+` 就变成静默的字符串
    粘连，脚本少几行、还看不出来错在哪 —— 这种脚本偏偏不能出错。
    """
    script = workdir / "apply-update.sh"
    pids = " ".join(str(pid) for pid in wait_pids if pid and pid > 1)
    template = """#!/bin/bash
# 自动更新：等旧进程退出后换包并重新打开。由 updater.py 生成，跑完即删。
set -u
LOG="{log}"
log() {{ printf '%s %s\\n' "$(date '+%F %T')" "$1" >>"$LOG"; }}
log "更新脚本启动，等待进程退出：{pids}"

# 慢慢等旧的自己退出。等不到也不能强杀 —— 万一它正在写配置文件。
for _ in $(seq 1 {wait_ticks}); do
  alive=0
  for p in {pids}; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ "$alive" = 0 ] && break
  sleep 0.5
done

# 兜底：让原生窗口应用自己走正常退出流程。
# 应用改过名，从旧版升级时跑的还是旧名的 App，新旧名字都要退出。
osascript -e 'tell application "{name}" to quit' >/dev/null 2>&1
{legacy_quit}
sleep 1

# 从网上下载的包带隔离标记，不清掉第一次打开会被 Gatekeeper 拦下
xattr -dr com.apple.quarantine "{new_app}" >/dev/null 2>&1

log "备份旧版本到 {backup}"
if ! mv "{old_app}" "{backup}" 2>>"$LOG"; then
  cp -R "{old_app}" "{backup}" 2>>"$LOG" && rm -rf "{old_app}" 2>>"$LOG"
fi

# 旧包没移走就绝不往下走：新旧文件混在一个包里比更新失败更难收拾
if [ -d "{old_app}" ]; then
  log "旧版本还在，放弃更新"
  osascript -e 'display alert "{name}" message "更新失败：没能移走旧版本，原程序保持不变。" as critical' >/dev/null 2>&1
  exit 1
fi

log "写入新版本"
if ! ditto "{new_app}" "{old_app}" 2>>"$LOG"; then
  log "复制失败，正在还原备份"
  ditto "{backup}" "{old_app}" 2>>"$LOG"
  osascript -e 'display alert "{name}" message "更新失败，已还原为原来的版本。" as critical' >/dev/null 2>&1
  open "{old_app}" >/dev/null 2>&1
  exit 1
fi

log "重新打开"
open "{old_app}" >/dev/null 2>&1
log "更新完成"
rm -rf "{workdir}" >/dev/null 2>&1
exit 0
"""
    legacy_quit = "".join(
        "osascript -e 'tell application \"%s\" to quit' >/dev/null 2>&1\n"
        % name.replace('"', '\\"') for name in LEGACY_APP_NAMES)
    script.write_text(template.format(
        log=paths.state_dir() / "update.log",
        pids=pids, wait_ticks=QUIT_WAIT_SECONDS * 2,
        name=APP_NAME.replace('"', '\\"'),
        legacy_quit=legacy_quit,
        new_app=new_app, old_app=old_app, backup=backup, workdir=workdir,
    ), encoding="utf-8")
    script.chmod(0o755)
    return script


def _gui_pid() -> int:
    try:
        return int(json.loads(paths.gui_state_file().read_text()).get("pid") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def plan() -> Dict:
    """只做检查、不下载：告诉界面"这个按钮能不能用、会装哪个包"。"""
    app = running_app_path()
    if app is None:
        return {"supported": False,
                "reason": "当前不是以 .app 方式运行（是从源码目录启动的）。"
                          "请到 GitHub 下载新版本覆盖安装。",
                "url": PROJECT_URL + "/releases/latest"}
    parent = app.parent
    if not _writable(parent):
        return {"supported": False,
                "reason": "没有权限写入 %s（装在 /Applications 时需要管理员权限）。"
                          "请手动下载新版本，或用完整版覆盖安装。" % parent,
                "url": PROJECT_URL + "/releases/latest"}
    if not shutil.which("ditto") and not shutil.which("unzip"):
        return {"supported": False,
                "reason": "本机没有 ditto / unzip，无法解压安装包。",
                "url": PROJECT_URL + "/releases/latest"}
    return {"supported": True, "app": str(app), "variant": install_variant(app),
            "asset": FULL_ASSET if install_variant(app) == "full" else STANDARD_ASSET,
            "current": __version__, "url": PROJECT_URL + "/releases/latest"}


def apply_update(expected_tag: str = "") -> Dict:
    """下载并安排替换。返回后本进程会退出，界面靠轮询判断新版本是否起来了。

    expected_tag：界面上看到的最新版本号。用它做一道**防降级**校验 ——
    下到的包比现在还旧就不换（比如 CDN 缓存了旧资产）。
    """
    check = plan()
    if not check.get("supported"):
        raise UpdateError(check.get("reason") or "当前环境不支持自动更新")

    app = Path(check["app"])
    variant = check["variant"]
    workdir = Path(tempfile.mkdtemp(prefix="codex-switcher-update-"))
    try:
        zip_path = workdir / "update.zip"
        _download(asset_url(variant), zip_path)
        new_app = _extract(zip_path, workdir)

        new_version = _version_of(new_app)
        if not new_version:
            raise UpdateError("下到的包里读不到版本号，已放弃更新。")
        if _numbers(new_version) < _numbers(__version__):
            raise UpdateError("下到的版本（v%s）比当前（v%s）还旧，已放弃更新。"
                              % (new_version, __version__))
        if expected_tag and _numbers(new_version) < _numbers(expected_tag):
            raise UpdateError("下到的版本（v%s）比界面显示的最新版（%s）旧，已放弃更新。"
                              % (new_version, expected_tag))
        if install_variant(new_app) != variant:
            raise UpdateError("下到的包版本类型不对（当前是%s版），已放弃更新。" % variant)

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # 注意要建的是 backup_dir 自己，不是它的父目录。之前建错了一层，
        # 备份时 mv 报 ENOENT —— 脚本会因此正确地中止更新，但结果是
        # 「永远更新不了，还看不出为什么」。
        backup_dir = paths.backups_dir() / ("app-pre-%s-%s" % (new_version, stamp))
        paths.ensure_dir(backup_dir)
        backup = backup_dir / app.name

        script = _write_script(app, new_app, backup, [os.getpid(), _gui_pid()], workdir)
        # 脱离本进程：start_new_session 让它不被我们退出时带走
        subprocess.Popen(["/bin/bash", str(script)],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        update_dir().mkdir(parents=True, exist_ok=True)
        (update_dir() / "pending.json").write_text(json.dumps({
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "from": __version__, "to": new_version, "app": str(app),
            "backup": str(backup),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "restarting", "from": __version__, "to": new_version,
                "backup": str(backup),
                "message": "正在更新到 v%s，应用会自动重启。" % new_version}
    except UpdateError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise UpdateError("更新失败（%s），原程序没有改动。" % type(exc).__name__)


def pending() -> Optional[Dict]:
    """上一次更新是否已经落地。用于界面在重启后回显结果。"""
    marker = update_dir() / "pending.json"
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    target = record.get("to")
    if not target:
        return None
    # 版本号已经是新的了 → 更新成功，把标记清掉
    if _numbers(__version__) >= _numbers(target):
        try:
            marker.unlink()
        except OSError:
            pass
        record["applied"] = True
        return record
    return None
