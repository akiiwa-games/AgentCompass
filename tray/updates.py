"""Update Dot2API, ChatGPT Web and the enabled Git plugins (Gemini Web, Muse2API).

    python updates.py check      fetch and show how far behind each clone is; changes nothing else
    python updates.py build      install dependencies and build the ChatGPT Web launcher (deploy.py install does this)
    python updates.py update     update Dot2API, ChatGPT Web and enabled Git plugins

Pulls are fast-forward only. ChatGPT Web's existing adapter edits are reversed selectively and rebuilt after
pulling. An incompatible update recovers the prior version and runtime for that component.
"""

from __future__ import annotations

from i18n import translate as _tr

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import chatgpt_web_patch as web_patch
import gateways
import plugins
import runtime_paths
import services

LOCK = threading.Lock()
_progress = threading.local()


def report(kind: str, message: str, component: str | None = None) -> None:
    callback = getattr(_progress, "callback", None)
    if callback:
        callback({"kind": kind, "message": message,
                  "component": component if component is not None else getattr(_progress, "component", "")})


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    report("progress", _tr('执行 {0} {1}').format(f'{Path(args[0]).name}', f"{' '.join(args[1:3])}"))
    process = subprocess.Popen(args, cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=services.CREATE_NO_WINDOW)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if process.poll() is None:
            services.kill_pid(process.pid)
        process.communicate()
        raise RuntimeError(_tr('{0} 超时或被中断，已停止本次子进程').format(f'{Path(args[0]).stem}')) from None
    output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if output:
        report("output", output[-6000:])
    if process.returncode != 0:
        important = [line for line in output.splitlines() if any(word in line.lower() for word in
                     ("conflict", "error:", "failed", "could not", "index was not", "patch does not"))]
        detail = "\n".join(important)[-1800:] or output[-600:]
        raise RuntimeError(_tr('{0} {1} 失败：{2}').format(f'{Path(args[0]).stem}', f"{' '.join(args[1:3])}", f'{detail}'))
    return stdout.strip()


def git(root: Path, *arguments: str, timeout: float = 120) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    return run(["git", *arguments], root, env=env, timeout=timeout)


def version(root: Path) -> str:
    return git(root, "describe", "--tags", "--always")


def behind(root: Path) -> int:
    """How many commits upstream has that this clone lacks, after fetching."""
    git(root, "fetch", "--tags", "origin", timeout=180)
    return int(git(root, "rev-list", "--count", "HEAD..@{upstream}"))


def changed(root: Path) -> list[str]:
    return git(root, "status", "--porcelain", "--untracked-files=no").splitlines()


def install_dependencies() -> None:
    bun = str(runtime_paths.bun_path())
    for directory in (services.CHATGPT_ROOT, services.CHATGPT_LAUNCHER):
        run([bun, "install", "--frozen-lockfile"], directory, env=services.chatgpt_env())


def build() -> None:
    """Dependencies, the launcher window, and the browser helper (which carries the patched source)."""
    bun = str(runtime_paths.bun_path())
    install_dependencies()
    run([bun, "run", "build:renderer"], services.CHATGPT_LAUNCHER, env=services.chatgpt_env())
    run([bun, "run", "scripts/build-browser-helper.ts"], services.CHATGPT_ROOT, env=services.chatgpt_env())


def patch_and_build() -> None:
    web_patch.apply(services.CHATGPT_ROOT)
    build()


def sync_dot2api() -> None:
    run([str(services.uv_path()), "sync", "--frozen", "--no-dev"], services.DOT_ROOT, env=services.dot_env())


def prepare_gateway(plugin_id: str) -> None:
    """Install Muse2API's dependencies after a clone or pull."""
    report("progress", _tr('构建 {0}（首次较慢）').format(f'{plugins.BY_ID[plugin_id].name}'), plugins.BY_ID[plugin_id].name)
    gateways.muse_sync(run, services.uv_path())


def ensure_plugin(plugin_id: str, root: Path | None = None) -> str:
    """Clone a missing Git plugin into ~/.route-manager/plugins/<id> and run its initial build/setup."""
    plugin = plugins.BY_ID[plugin_id]
    if not plugin.repo:
        return _tr('{0} 无远端仓库').format(f'{plugin.name}')
    target = root or (plugins.PLUGINS_DIR / plugin_id)
    if (target / ".git").exists():
        return _tr('{0} 已部署（{1}）').format(f'{plugin.name}', f'{version(target)}')
    target.parent.mkdir(parents=True, exist_ok=True)
    report("progress", _tr('从 GitHub 下载 {0} 到 {1}').format(f'{plugin.name}', f'{target}'), plugin.name)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    run(["git", "clone", plugin.repo, str(target)], target.parent, env=env, timeout=600)
    services.refresh_plugin_roots()
    if plugin_id == "dot2api":
        sync_dot2api()
    elif plugin_id == "chatgpt-web":
        patch_and_build()
    elif plugin_id == "muse2api":
        prepare_gateway(plugin_id)
    return _tr('{0} 已下载并部署（{1}）').format(f'{plugin.name}', f'{version(target)}')


def update_simple_plugin(plugin_id: str, root: Path) -> str:
    plugin = plugins.BY_ID[plugin_id]
    if not (root / ".git").exists():
        return ensure_plugin(plugin_id, root)
    if behind(root) == 0:
        return _tr('{0} 已是最新（{1}）').format(f'{plugin.name}', f'{version(root)}')
    if changed(root):
        return _tr('{0} 有本地改动，未更新').format(f'{plugin.name}')
    old = version(root)
    git(root, "pull", "--ff-only", timeout=300)
    return _tr('{0} 已更新：{1} → {2}').format(f'{plugin.name}', f'{old}', f'{version(root)}')


def update_usage_float() -> str:
    return update_simple_plugin("usage-float", services.USAGE_FLOAT_ROOT)


def update_gemini_web() -> str:
    return update_simple_plugin("gemini-web", services.GEMINI_ROOT)


def update_dot2api() -> str:
    root = services.DOT_ROOT
    if not (root / ".git").exists():
        return ensure_plugin("dot2api", root)
    if behind(root) == 0:
        return _tr('Dot2API 已是最新（{0}）').format(f'{version(root)}')
    if changed(root):
        return _tr("Dot2API 有本地改动，未更新")
    before, old = git(root, "rev-parse", "HEAD"), version(root)
    was_running = services.dot_ready()
    if was_running:
        services.stop_dot2api(tunnel=False)    # the tunnel stays up: a new one means a new public address
    try:
        git(root, "pull", "--ff-only", timeout=300)
        sync_dot2api()
    except Exception as error:
        message = _tr('Dot2API 更新失败：{0}').format(f'{error}')
        try:
            git(root, "reset", "--hard", before)
            sync_dot2api()
            message += _tr("；已回到原版本")
        except Exception as again:
            message += _tr('；回退也失败了：{0}').format(f'{again}')
        if was_running:
            services.start_dot2api()
        return message
    if was_running:
        services.start_dot2api()
    return _tr('Dot2API 已更新：{0} → {1}').format(f'{old}', f'{version(root)}')


def update_gateway(plugin_id: str) -> str:
    """Pull Muse2API, rebuild, and restart it if it was running; roll back on a failed build."""
    name = plugins.BY_ID[plugin_id].name
    root = plugins.plugin_root(plugin_id)
    if not (root / ".git").exists():
        return ensure_plugin(plugin_id, root)
    if behind(root) == 0:
        if not gateways.built(plugin_id):
            prepare_gateway(plugin_id)
            return _tr('{0} 已补完构建（{1}）').format(f'{name}', f'{version(root)}')
        return _tr('{0} 已是最新（{1}）').format(f'{name}', f'{version(root)}')
    if changed(root):
        return _tr('{0} 有本地改动，未更新').format(f'{name}')
    before, old = git(root, "rev-parse", "HEAD"), version(root)
    was_running = services.muse_ready()
    services.UPDATING_COMPONENTS.add(plugin_id)
    try:
        if was_running:
            services.plugin_stop(plugin_id)
        try:
            git(root, "pull", "--ff-only", timeout=300)
            prepare_gateway(plugin_id)
            message = _tr('{0} 已更新：{1} → {2}').format(f'{name}', f'{old}', f'{version(root)}')
        except Exception as error:
            message = _tr('{0} 更新失败：{1}').format(f'{name}', f'{error}')
            try:
                git(root, "reset", "--hard", before)
                prepare_gateway(plugin_id)
                message += _tr("；已回到原版本")
            except Exception as again:
                message += _tr('；回退也失败了：{0}').format(f'{again}')
    finally:
        services.UPDATING_COMPONENTS.discard(plugin_id)
    if was_running:
        services.plugin_start(plugin_id)
    return message


def deploy_gateway(plugin_id: str, start: bool, progress=None) -> str:
    """On enabling Muse2API: clone and build it (reported to the update window), then start it if asked."""
    name = plugins.BY_ID[plugin_id].name
    if not LOCK.acquire(blocking=False):
        return _tr("正在更新，请稍候再启用")
    try:
        _progress.callback, _progress.component = progress, name
        report("start", _tr('部署 {0}').format(f'{name}'), "")
        messages = []
        root = plugins.plugin_root(plugin_id)
        if not (root / ".git").exists():
            messages.append(ensure_plugin(plugin_id))
        elif not gateways.built(plugin_id):
            prepare_gateway(plugin_id)
            messages.append(_tr('{0} 已补完构建').format(f'{name}'))
        if start:
            messages.append(services.plugin_start(plugin_id))
        result = "；".join(messages) or _tr('{0} 已部署').format(f'{name}')
        report("result", result)
        report("finish", _tr('{0} 部署结束').format(f'{name}'), "")
        return result
    except Exception as error:
        report("result", _tr('{0} 部署失败：{1}').format(f'{name}', f'{error}'))
        report("finish", _tr('{0} 部署结束').format(f'{name}'), "")
        raise
    finally:
        _progress.callback = None
        _progress.component = ""
        LOCK.release()


def update_chatgpt_web() -> str:
    root = services.CHATGPT_ROOT
    if not (root / ".git").exists():
        return ensure_plugin("chatgpt-web", root)
    if services.chatgpt_busy():
        return _tr("ChatGPT Web 正在处理请求，稍后再更新")
    if behind(root) == 0:
        return _tr('ChatGPT Web 已是最新（{0}）').format(f'{version(root)}')
    was_running = services.chatgpt_main_pid() is not None
    if was_running:
        stopped = services.stop_chatgpt_web()
        if services.chatgpt_main_pid() is not None:
            return _tr('ChatGPT Web 没能停下，未更新：{0}').format(f'{stopped}')
    before, old = git(root, "rev-parse", "HEAD"), version(root)
    try:
        web_patch.revert(root)
        if changed(root):
            raise RuntimeError(_tr("工程里有补丁之外的本地改动"))
        git(root, "pull", "--ff-only", timeout=300)
        patch_and_build()
    except Exception as error:
        message = _tr('ChatGPT Web 更新失败：{0}').format(f'{error}')
        try:
            if git(root, "rev-parse", "HEAD") != before:
                git(root, "reset", "--hard", before)
            patch_and_build()
            message += _tr("；已回到原版本")
        except Exception as again:
            message += _tr('；回退也失败了：{0}').format(f'{again}')
        if was_running:
            services.start_chatgpt_web()
        return message
    if was_running:
        services.start_chatgpt_web()
    return _tr('ChatGPT Web 已更新：{0} → {1}').format(f'{old}', f'{version(root)}')


def update_all(progress=None) -> str:
    if not LOCK.acquire(blocking=False):
        return _tr("正在更新，请稍候")
    try:
        _progress.callback = progress
        report("start", _tr("开始检查并更新已启用插件组件"), "")
        results = []
        config = plugins.load()
        for label, update, plugin_id in (("Dot2API", update_dot2api, "dot2api"),
                                         ("ChatGPT Web", update_chatgpt_web, "chatgpt-web"),
                                         ("Usage Float", update_usage_float, "usage-float"),
                                         ("Gemini Web", update_gemini_web, "gemini-web"),
                                         ("Muse2API", lambda: update_gateway("muse2api"), "muse2api")):
            _progress.component = label
            if plugin_id and not plugins.enabled(plugin_id, config):
                if not plugins.BY_ID[plugin_id].enabled_by_default:
                    continue    # an opt-in plugin the user never enabled is not mentioned
                result = _tr('{0} 插件未启用，已跳过').format(f'{label}')
                results.append(result)
                report("result", result)
                continue
            report("component", _tr("检查上游版本"))
            try:
                result = update()
            except Exception as error:
                result = _tr('{0} 更新出错：{1}').format(f'{label}', f'{error}')
            results.append(result)
            report("result", result)
        notice = product_notice()
        if notice:
            results.append(notice)
            report("result", notice)
        report("finish", _tr("本轮更新检查结束"), "")
        return "；".join(results)
    finally:
        _progress.callback = None
        _progress.component = ""
        LOCK.release()


def local_version(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1]
    init = root / "agentcompass" / "__init__.py"
    if init.is_file():
        import re
        match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', init.read_text(encoding="utf-8"), re.M)
        if match:
            return match.group(1)
    return "0.0.0"


def _version_key(text: str) -> tuple[int, ...]:
    text = text.strip().lstrip("vV")
    parts: list[int] = []
    for piece in text.split("."):
        number = ""
        for char in piece:
            if char.isdigit():
                number += char
            else:
                break
        if number == "":
            break
        parts.append(int(number))
    return tuple(parts) or (0,)


def release_notice(root: Path, opener=None) -> str:
    """Tell a non-git install how to upgrade. Does not download or replace files."""
    if str(root) not in os.sys.path:
        os.sys.path.append(str(root))
    from agentcompass.origin import RELEASES_API, RELEASES_PAGE

    local = local_version(root)
    request = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "agentcompass"},
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return (
            f"AgentCompass {local} has no .git checkout, so it cannot git pull. "
            f"Could not read GitHub releases ({error}). "
            f"Upgrade with: uv tool upgrade agentcompass "
            f"or download the installer from {RELEASES_PAGE}"
        )
    tag = str(payload.get("tag_name") or "")
    page = str(payload.get("html_url") or RELEASES_PAGE)
    remote = _version_key(tag)
    here = _version_key(local)
    if remote > here:
        return (
            f"AgentCompass {local} is not a git checkout. GitHub release {tag} is newer. "
            f"Upgrade the router with: uv tool upgrade agentcompass "
            f"Or download the Windows installer from: {page}"
        )
    if remote == here:
        return f"AgentCompass {local} matches GitHub release {tag}. No upgrade is waiting."
    return f"AgentCompass {local} is newer than GitHub release {tag or 'unknown'}."


def product_notice(root: Path | None = None, opener=None) -> str:
    """Empty for a dev checkout, so the existing git updater is unchanged."""
    root = root or Path(__file__).resolve().parents[1]
    if (root / ".git").exists():
        return ""
    try:
        return release_notice(root, opener=opener)
    except Exception as error:
        return (
            f"AgentCompass has no git checkout and the release check failed: {error}. "
            "Upgrade with: uv tool upgrade agentcompass "
            "or download the installer from https://github.com/akiiwa-games/AgentCompass/releases/latest"
        )


def dev_checkout_line(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        return ""
    try:
        described = git(root, "describe", "--tags", "--always")
    except Exception as error:
        described = f"unknown ({error})"
    return f"AgentCompass is a dev checkout ({described}). Update it with git pull in {root}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "build", "update"))
    action = parser.parse_args().action
    if action == "build":
        build()
        print(_tr("已构建"))
    elif action == "update":
        from update_status import record
        print(update_all(progress=record))
    else:
        root = Path(__file__).resolve().parents[1]
        if (root / ".git").exists():
            print(dev_checkout_line(root))
        else:
            print(product_notice(root))
        for name, root in (("Dot2API", services.DOT_ROOT), ("ChatGPT Web", services.CHATGPT_ROOT),
                           ("Usage Float", services.USAGE_FLOAT_ROOT)):
            count = behind(root)
            print(f"{name}: {version(root)}" + (_tr('，落后上游 {0} 个提交').format(f'{count}') if count else _tr("，已是最新")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
