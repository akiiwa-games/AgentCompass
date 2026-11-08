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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

import chatgpt_web_patch as web_patch
import component_launch
import gateways
import plugins
import runtime_paths
import services

LOCK = threading.Lock()
_progress = threading.local()
RELEASE_CHECK_INTERVAL_SECONDS = 6 * 3600
RELEASE_CACHE_NAME = "product-release.json"


def report(kind: str, message: str, component: str | None = None, **extra) -> None:
    callback = getattr(_progress, "callback", None)
    if callback:
        callback({"kind": kind, "message": message,
                  "component": component if component is not None else getattr(_progress, "component", ""),
                  **extra})


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 600) -> str:
    report("progress", _tr('执行 {0} {1}').format(f'{Path(args[0]).name}', f"{' '.join(args[1:3])}"))
    safe_work_dir = services.safe_cwd(cwd)
    process = subprocess.Popen(args, cwd=str(safe_work_dir), env=env, text=True, encoding="utf-8", errors="replace",
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
    # Upstream tags win: a plugin clone is not where tags are made, and a tag upstream re-pointed (a rewritten
    # history) would otherwise be rejected as "would clobber existing tag" on every update from then on.
    git(root, "fetch", "--tags", "--force", "origin", timeout=180)
    return int(git(root, "rev-list", "--count", "HEAD..@{upstream}"))


def changed(root: Path) -> list[str]:
    return git(root, "status", "--porcelain", "--untracked-files=no").splitlines()


def install_dependencies() -> None:
    bun = str(runtime_paths.bun_path())
    root = services.CHATGPT_ROOT.resolve()
    launcher = services.CHATGPT_LAUNCHER.resolve()
    for directory in (root, launcher):
        run([bun, "install", "--frozen-lockfile"], directory, env=services.chatgpt_env())
    if not services.chatgpt_exe().is_file() and (launcher / "node_modules" / "electron" / "install.js").is_file():
        run([bun, "run", "node_modules/electron/install.js"], launcher, env=services.chatgpt_env())


def build() -> None:
    """Dependencies, the launcher window, and the browser helper (which carries the patched source)."""
    bun = str(runtime_paths.bun_path())
    root = services.CHATGPT_ROOT.resolve()
    launcher = services.CHATGPT_LAUNCHER.resolve()
    install_dependencies()
    run([bun, "run", "build:renderer"], launcher, env=services.chatgpt_env())
    run([bun, "run", "scripts/build-browser-helper.ts"], root, env=services.chatgpt_env())


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
    """Clone a missing Git plugin into ~/.agent-compass/plugins/<id> and run its initial build/setup."""
    plugin = plugins.BY_ID[plugin_id]
    if not plugin.repo:
        return _set_status("skipped", _tr('{0} 无远端仓库').format(f'{plugin.name}'))
    target = root or (plugins.PLUGINS_DIR / plugin_id)
    if (target / ".git").exists():
        return _set_status("current", _tr('{0} 已部署（{1}）').format(f'{plugin.name}', f'{version(target)}'))
    target.parent.mkdir(parents=True, exist_ok=True)
    report("progress", _tr('从 GitHub 下载 {0} 到 {1}').format(f'{plugin.name}', f'{target}'), plugin.name)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    repo = plugins._sibling_repo("usage-float") if plugin_id == "usage-float" else plugin.repo
    run(["git", "clone", repo, str(target)], target.parent, env=env, timeout=600)
    services.refresh_plugin_roots()
    if plugin_id == "dot2api":
        sync_dot2api()
    elif plugin_id == "chatgpt-web":
        patch_and_build()
    elif plugin_id == "muse2api":
        prepare_gateway(plugin_id)
    return _set_status("updated", _tr('{0} 已下载并部署（{1}）').format(f'{plugin.name}', f'{version(target)}'))


def update_simple_plugin(plugin_id: str, root: Path) -> str:
    plugin = plugins.BY_ID[plugin_id]
    if not (root / ".git").exists():
        return ensure_plugin(plugin_id, root)
    if behind(root) == 0:
        return _set_status("current", _tr('{0} 已是最新（{1}）').format(f'{plugin.name}', f'{version(root)}'))
    if changed(root):
        return _set_status("failed", _tr('{0} 有本地改动，未更新').format(f'{plugin.name}'))
    old = version(root)
    git(root, "pull", "--ff-only", timeout=300)
    return _set_status("updated", _tr('{0} 已更新：{1} → {2}').format(f'{plugin.name}', f'{old}', f'{version(root)}'))


def update_usage_float() -> str:
    return update_simple_plugin("usage-float", services.USAGE_FLOAT_ROOT)


def update_gemini_web() -> str:
    return update_simple_plugin("gemini-web", services.GEMINI_ROOT)


def update_dot2api() -> str:
    root = services.DOT_ROOT
    if not (root / ".git").exists():
        return ensure_plugin("dot2api", root)
    if behind(root) == 0:
        return _set_status("current", _tr('Dot2API 已是最新（{0}）').format(f'{version(root)}'))
    if changed(root):
        return _set_status("failed", _tr("Dot2API 有本地改动，未更新"))
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
        return _set_status("failed", message)
    if was_running:
        services.start_dot2api()
    return _set_status("updated", _tr('Dot2API 已更新：{0} → {1}').format(f'{old}', f'{version(root)}'))


def update_gateway(plugin_id: str) -> str:
    """Pull Muse2API, rebuild, and restart it if it was running; roll back on a failed build."""
    name = plugins.BY_ID[plugin_id].name
    root = plugins.plugin_root(plugin_id)
    if not (root / ".git").exists():
        return ensure_plugin(plugin_id, root)
    if behind(root) == 0:
        if not gateways.built(plugin_id):
            prepare_gateway(plugin_id)
            return _set_status("updated", _tr('{0} 已补完构建（{1}）').format(f'{name}', f'{version(root)}'))
        return _set_status("current", _tr('{0} 已是最新（{1}）').format(f'{name}', f'{version(root)}'))
    if changed(root):
        return _set_status("failed", _tr('{0} 有本地改动，未更新').format(f'{name}'))
    before, old = git(root, "rev-parse", "HEAD"), version(root)
    was_running = services.muse_ready()
    services.UPDATING_COMPONENTS.add(plugin_id)
    try:
        if was_running:
            services.plugin_stop(plugin_id)
        try:
            git(root, "pull", "--ff-only", timeout=300)
            prepare_gateway(plugin_id)
            message = _set_status("updated", _tr('{0} 已更新：{1} → {2}').format(f'{name}', f'{old}', f'{version(root)}'))
        except Exception as error:
            message = _set_status("failed", _tr('{0} 更新失败：{1}').format(f'{name}', f'{error}'))
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
        return _set_status("deferred", _tr("ChatGPT Web 正在处理请求，稍后再更新"))
    if behind(root) == 0:
        return _set_status("current", _tr('ChatGPT Web 已是最新（{0}）').format(f'{version(root)}'))
    was_running = services.chatgpt_main_pid() is not None
    if was_running:
        stopped = services.stop_chatgpt_web()
        if services.chatgpt_main_pid() is not None:
            return _set_status("failed", _tr('ChatGPT Web 没能停下，未更新：{0}').format(f'{stopped}'))
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
        return _set_status("failed", message)
    if was_running:
        services.start_chatgpt_web()
    return _set_status("updated", _tr('ChatGPT Web 已更新：{0} → {1}').format(f'{old}', f'{version(root)}'))


def _set_status(status: str, message: str) -> str:
    _progress.status = status
    return message


def _infer_mock_status(message: str) -> str:
    """Fallback when a unit test patches a component updater with a plain return_value string."""
    lower = str(message or "").lower()
    if any(w in lower for w in ("失败", "出错", "未更新", "没能", "failed", "error", "not updated", "could not")):
        return "failed"
    if any(w in lower for w in ("稍后", "延后", "正在处理", "later", "deferred", "busy")):
        return "deferred"
    if any(w in lower for w in ("已跳过", "skipped")):
        return "skipped"
    if any(w in lower for w in ("已更新", "已下载", "已补完", "updated", "deployed")):
        return "updated"
    return "current"


class ProductNoticeResult(str):
    """Product release notice string carrying structured `status` ('newer' | 'current' | 'ahead' | 'failed' | 'dev')."""

    status: str
    kind: str

    def __new__(cls, text: str = "", *, status: str = "dev"):
        obj = super().__new__(cls, text)
        obj.status = str(status)
        obj.kind = str(status)
        return obj


class UpdateAllResult(str):
    """Summary string with structured component outcomes (`busy`, `components`, `statuses`, `product_notice`, `product_status`)."""

    busy: bool
    summary: str
    product_notice: str
    product_status: str
    components: dict[str, dict[str, str]]
    statuses: dict[str, str]
    updated: list[str]
    current: list[str]
    skipped: list[str]
    deferred: list[str]
    failed: list[str]
    messages: list[str]

    def __new__(
        cls,
        summary: str = "",
        *,
        busy: bool = False,
        components: dict[str, dict[str, str]] | None = None,
        product_notice: str = "",
        product_status: str | None = None,
    ):
        obj = super().__new__(cls, summary)
        comp = dict(components or {})
        statuses = {
            name: str(info.get("status", "failed") if isinstance(info, dict) else info)
            for name, info in comp.items()
        }
        resolved_product_status = (
            product_status
            or getattr(product_notice, "status", None)
            or getattr(product_notice, "kind", None)
            or ("dev" if not product_notice else "current")
        )
        obj.summary = str(summary)
        obj.busy = bool(busy)
        obj.components = comp
        obj.statuses = statuses
        obj.product_notice = str(product_notice or "")
        obj.product_status = str(resolved_product_status)
        obj.updated = [name for name, st in statuses.items() if st == "updated"]
        obj.current = [name for name, st in statuses.items() if st == "current"]
        obj.skipped = [name for name, st in statuses.items() if st == "skipped"]
        obj.deferred = [name for name, st in statuses.items() if st == "deferred"]
        obj.failed = [name for name, st in statuses.items() if st == "failed"]
        obj.messages = [
            str(info.get("message", "")) for info in comp.values() if isinstance(info, dict) and info.get("message")
        ]
        if obj.product_notice:
            obj.messages.append(obj.product_notice)
        return obj

    def _as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "busy": self.busy,
            "components": self.components,
            "statuses": self.statuses,
            "product_notice": self.product_notice,
            "product_status": self.product_status,
            "updated": self.updated,
            "current": self.current,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "failed": self.failed,
            "messages": self.messages,
        }

    def get(self, key: str, default=None):
        return self._as_dict().get(key, default)

    def keys(self):
        return self._as_dict().keys()

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return super().__getitem__(key)
        return self._as_dict()[key]


def update_all(progress=None) -> UpdateAllResult:
    if not LOCK.acquire(blocking=False):
        return UpdateAllResult(_tr("正在更新，请稍候"), busy=True)
    try:
        _progress.callback = progress
        _progress.status = None
        config = plugins.load()
        items = (("Dot2API", update_dot2api, "dot2api"),
                 ("ChatGPT Web", update_chatgpt_web, "chatgpt-web"),
                 ("Usage Float", update_usage_float, "usage-float"),
                 ("Gemini Web", update_gemini_web, "gemini-web"),
                 ("Muse2API", lambda: update_gateway("muse2api"), "muse2api"))
        targets = [label for label, _, pid in items if plugins.enabled(pid, config) or plugins.BY_ID[pid].enabled_by_default]
        report("start", _tr("开始检查并更新已启用插件组件"), "", title=_tr("外部集成"), targets=targets)
        results = []
        components: dict[str, dict[str, str]] = {}
        for label, update, plugin_id in items:
            _progress.component = label
            _progress.status = None
            if plugin_id and not plugins.enabled(plugin_id, config):
                if not plugins.BY_ID[plugin_id].enabled_by_default:
                    continue    # an opt-in plugin the user never enabled is not mentioned
                result = _tr('{0} 插件未启用，已跳过').format(f'{label}')
                results.append(result)
                components[label] = {"status": "skipped", "message": result}
                report("result", result)
                continue
            report("component", _tr("检查上游版本"))
            try:
                result = update()
                status = getattr(_progress, "status", None) or _infer_mock_status(result)
            except Exception as error:
                result = _tr('{0} 更新出错：{1}').format(f'{label}', f'{error}')
                status = "failed"
            results.append(result)
            components[label] = {"status": status, "message": result}
            report("result", result)
        notice = product_notice()
        product_status = str(
            getattr(notice, "status", None)
            or getattr(notice, "kind", None)
            or ("dev" if not notice else "current")
        )
        if notice:
            results.append(notice)
            report("result", notice, "AgentCompass")
        report("finish", _tr("本轮更新检查结束"), "")
        return UpdateAllResult(
            "；".join(results),
            busy=False,
            components=components,
            product_notice=notice,
            product_status=product_status,
        )
    finally:
        _progress.callback = None
        _progress.component = ""
        _progress.status = None
        LOCK.release()


def _read_version_from_json(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    if isinstance(payload, dict):
        ver = str(payload.get("version") or "").strip().lstrip("vV")
        if ver:
            return ver
    return ""


def local_version(root: Path | None = None) -> str:
    if root is None and component_launch.frozen():
        exe_dir = Path(sys.executable).resolve().parent
        disk_ver = local_version(exe_dir)
        if disk_ver and disk_ver != "0.0.0":
            return disk_ver
        try:
            import agentcompass
            ver = getattr(agentcompass, "__version__", "")
            if ver:
                return str(ver)
        except Exception:
            pass
    search = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    for app_manifest in (search / "app" / "current" / "app-manifest.json", search / "app-manifest.json"):
        ver = _read_version_from_json(app_manifest)
        if ver:
            return ver
    for init in (
        search / "app" / "current" / "agentcompass" / "__init__.py",
        search / "agentcompass" / "__init__.py",
        search / "__init__.py",
    ):
        if init.is_file():
            match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', init.read_text(encoding="utf-8", errors="replace"), re.M)
            if match:
                return match.group(1)
    for manifest_json in (search / "manifest.json", search / "app" / "state.json", search / "runtime-info.json"):
        if manifest_json.name == "state.json" and manifest_json.is_file():
            try:
                state_payload = json.loads(manifest_json.read_text(encoding="utf-8", errors="replace"))
                if isinstance(state_payload, dict):
                    cur_ver = str(
                        state_payload.get("current_version")
                        or state_payload.get("active_version")
                        or ""
                    ).strip().lstrip("vV")
                    if cur_ver:
                        return cur_ver
            except Exception:
                pass
        elif manifest_json.name == "runtime-info.json" and manifest_json.is_file():
            try:
                rt_payload = json.loads(manifest_json.read_text(encoding="utf-8", errors="replace"))
                if isinstance(rt_payload, dict):
                    rt_ver = str(rt_payload.get("runtime_version") or rt_payload.get("version") or "").strip().lstrip("vV")
                    if rt_ver:
                        return rt_ver
            except Exception:
                pass
        else:
            ver = _read_version_from_json(manifest_json)
            if ver:
                return ver
    for base in (search, search.parent):
        for meta in sorted(base.glob("agentcompass-*.dist-info/METADATA")):
            if meta.is_file():
                match = re.search(r"^Version:\s*(\S+)", meta.read_text(encoding="utf-8", errors="replace"), re.M)
                if match:
                    return match.group(1).strip()
    if root is None:
        try:
            import agentcompass
            ver = getattr(agentcompass, "__version__", "")
            if ver:
                return str(ver)
        except Exception:
            pass
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


def _norm_root(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _is_installer_dir(path: Path) -> bool:
    """The installer's folder (tray exe, uninstaller, or runtime metadata). Not its cli/ subfolder: file names
    ignore case, so cli/agentcompass.exe would pass for AgentCompass.exe."""
    if path.name.lower() == "cli" and _is_installer_dir(path.parent):
        return False
    return (
        (path / "AgentCompass.exe").is_file()
        or (path / "unins000.exe").is_file()
        or (path / "runtime-info.json").is_file()
    )


def _installer_version(root: Path) -> str:
    """Since 0.4.0, installer folders keep the active code package under app/current/ (with app-manifest.json and
    agentcompass/__init__.py) plus runtime-info.json at the install root, so local_version(root) reads the installed
    version directly on disk even from non-frozen processes. Fall back to the running process version or cli --version
    only for legacy (<0.4.0) single-bundle installs that had no version file on disk."""
    ver = local_version(root)
    if ver == "0.0.0" and component_launch.frozen() and _norm_root(Path(sys.executable).parent) == _norm_root(root):
        ver = local_version()
    if ver == "0.0.0":
        cli_exe = Path(root) / "cli" / ("agentcompass.exe" if os.name == "nt" else "agentcompass")
        try:
            if cli_exe.is_file() and cli_exe.stat().st_size > 4096:
                res = subprocess.run(
                    [str(cli_exe), "--version"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=5,
                    creationflags=services.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    check=False,
                )
                if res.returncode == 0:
                    match = re.search(r"(\d+\.\d+\.\d+)", res.stdout or "")
                    if match:
                        return match.group(1)
        except Exception:
            pass
    return ver


def detect_install_shape(root: Path | None = None, frozen: bool | None = None) -> str:
    """Return 'installer', 'wheel', or 'git' for the given installation root."""
    if frozen is None:
        frozen = component_launch.frozen() if root is None else False
    if frozen:
        return "installer"
    target = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[1]
    for current in (target, *target.parents):
        if (current / ".git").exists() and (
            (current / "agentcompass").is_dir() or (current / "tray").is_dir() or current == target
        ):
            return "git"
        if _is_installer_dir(current):
            return "installer"
        if list(current.glob("agentcompass-*.dist-info")):
            return "wheel"
    if (target / ".git").exists():
        return "git"
    return "wheel"


def _find_python_for_site_packages(sp: Path, python_hint: Path | None = None) -> Path | None:
    if python_hint is not None and Path(python_hint).is_file():
        return Path(python_hint).resolve()
    candidates = [
        sp.parent.parent / "Scripts" / "python.exe",
        sp.parent.parent / "python.exe",
        sp.parent.parent / "bin" / "python",
        sp.parent.parent.parent / "bin" / "python",
        sp.parent / "Scripts" / "python.exe",
        sp.parent / "python.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _install_info_from_path(target: Path, python_hint: Path | None = None) -> dict | None:
    try:
        resolved = Path(target).resolve()
    except OSError:
        resolved = Path(target)
    chain = [resolved, *resolved.parents]
    for current in chain:
        dist_infos = sorted(current.glob("agentcompass-*.dist-info"))
        if dist_infos:
            dist_info = dist_infos[-1]
            ver = "0.0.0"
            meta = dist_info / "METADATA"
            if meta.is_file():
                match = re.search(r"^Version:\s*(\S+)", meta.read_text(encoding="utf-8", errors="replace"), re.M)
                if match:
                    ver = match.group(1).strip()
            if ver == "0.0.0":
                ver = local_version(current)
            installer_file = dist_info / "INSTALLER"
            installer_tool = (
                installer_file.read_text(encoding="utf-8", errors="replace").strip().lower()
                if installer_file.is_file()
                else "pip"
            ) or "pip"
            direct_url_file = dist_info / "direct_url.json"
            direct_url = None
            if direct_url_file.is_file():
                try:
                    direct_url = json.loads(direct_url_file.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    direct_url = None
            py_exe = _find_python_for_site_packages(current, python_hint=python_hint)
            return {
                "shape": "wheel",
                "root": current.resolve(),
                "version": ver,
                "installer": installer_tool,
                "direct_url": direct_url,
                "python": str(py_exe) if py_exe else None,
                "dist_info": dist_info,
            }
    for current in chain:
        if (current / ".git").exists() and (
            (current / "agentcompass" / "__init__.py").is_file()
            or (current / "tray" / "app.pyw").is_file()
            or current == resolved
        ):
            return {
                "shape": "git",
                "root": current.resolve(),
                "version": local_version(current),
                "installer": "git",
                "direct_url": None,
                "python": None,
            }
        if _is_installer_dir(current):
            return {
                "shape": "installer",
                "root": current.resolve(),
                "version": _installer_version(current),
                "installer": "installer",
                "direct_url": None,
                "python": None,
            }
    for current in chain:
        if (current / "agentcompass" / "__init__.py").is_file():
            py_exe = _find_python_for_site_packages(current, python_hint=python_hint)
            return {
                "shape": "wheel",
                "root": current.resolve(),
                "version": local_version(current),
                "installer": "pip",
                "direct_url": None,
                "python": str(py_exe) if py_exe else None,
            }
    return None


def _resolve_cli_on_path(which_path: str | Path | bool | None = False) -> dict | None:
    if which_path is None:
        return None
    found = str(which_path) if which_path is not False else shutil.which("agentcompass")
    if not found:
        return None
    exe = Path(found)
    if not exe.is_file():
        return None
    python_hint: Path | None = None
    try:
        raw = exe.read_bytes()
        shebangs = re.findall(rb"#!([^\r\n]+)", raw)
        if shebangs:
            candidate_str = shebangs[-1].decode("utf-8", errors="replace").strip().strip('"')
            cand_path = Path(candidate_str)
            if cand_path.is_file():
                python_hint = cand_path
    except OSError:
        pass
    if python_hint is None:
        for cand in (
            exe.parent / "python.exe",
            exe.parent / "python",
            exe.parent.parent / "python.exe",
            exe.parent.parent / "Scripts" / "python.exe",
        ):
            if cand.is_file():
                python_hint = cand
                break
    search_bases: list[Path] = []
    if python_hint is not None:
        search_bases.extend([python_hint.parent, python_hint.parent.parent])
    try:
        exe_resolved = exe.resolve()
    except OSError:
        exe_resolved = exe
    search_bases.extend([exe_resolved.parent, exe_resolved.parent.parent])
    for base in search_bases:
        sp_candidates = [
            base / "Lib" / "site-packages",
            base / "site-packages",
            *(base.glob("lib/python*/site-packages") if base.is_dir() else ()),
            base,
        ]
        for sp in sp_candidates:
            if sp.is_dir() and (
                list(sp.glob("agentcompass-*.dist-info")) or (sp / "agentcompass" / "__init__.py").is_file()
            ):
                info = _install_info_from_path(sp, python_hint=python_hint)
                if info:
                    info["cli"] = str(exe)
                    if python_hint and not info.get("python"):
                        info["python"] = str(python_hint)
                    return info
    info = _install_info_from_path(exe_resolved, python_hint=python_hint)
    if info:
        info["cli"] = str(exe)
    return info


def inspect_installs(
    root: Path | None = None,
    home: Path | None = None,
    which_path: str | Path | bool | None = False,
    frozen: bool | None = None,
) -> dict:
    """Inspect the tray install and the router installs resolved via skills, gate hooks, and PATH."""
    is_frozen = frozen if frozen is not None else (component_launch.frozen() if root is None else False)
    if is_frozen:
        exe_dir = Path(root).resolve() if root is not None else Path(sys.executable).resolve().parent
        tray = {
            "shape": "installer",
            "root": exe_dir,
            "version": local_version(root),
            "installer": "installer",
            "direct_url": None,
            "python": None,
        }
    else:
        tray_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[1]
        tray = _install_info_from_path(tray_root) or {
            "shape": detect_install_shape(tray_root, frozen=False),
            "root": tray_root,
            "version": local_version(tray_root),
            "installer": "git" if (tray_root / ".git").exists() else "pip",
            "direct_url": None,
            "python": None,
        }

    home_dir = Path(home) if home is not None else Path.home()
    codex_home = (
        Path(os.environ["CODEX_HOME"])
        if (home is None and os.environ.get("CODEX_HOME"))
        else (home_dir / ".codex")
    )
    claude_home = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        if (home is None and os.environ.get("CLAUDE_CONFIG_DIR"))
        else (home_dir / ".claude")
    )
    gemini_home = (
        Path(os.environ["GEMINI_HOME"])
        if (home is None and os.environ.get("GEMINI_HOME"))
        else (home_dir / ".gemini")
    )

    routers: dict[str, dict] = {}

    # 1. router_priority.modules() resolution (CODEX_HOME/skills/agent-compass/scripts)
    try:
        import router_priority
        if hasattr(router_priority.modules, "_mock_name"):
            mods = router_priority.modules()
            mod_file = getattr(mods[0], "__file__", None) if mods else None
            if mod_file:
                info = _install_info_from_path(Path(mod_file))
                if info:
                    routers["skills"] = info
    except Exception:
        pass
    if "skills" not in routers:
        scripts_dir = codex_home / "skills" / "agent-compass" / "scripts"
        for script_name in ("route_judge.py", "allocator.py"):
            script_file = scripts_dir / script_name
            if script_file.is_file():
                info = _install_info_from_path(script_file)
                if info:
                    routers["skills"] = info
                    break

    # 2. Gate hooks resolution
    hook_files = (
        claude_home / "settings.json",
        codex_home / "hooks.json",
        gemini_home / "settings.json",
        home_dir / ".pi" / "agent" / "extensions" / "router-gate.ts",
    )
    # The script path of a wheel or checkout, or the Windows installer's `<app>/cli/agentcompass.exe gate-hook`.
    hook_pattern = re.compile(r'([A-Za-z]:[\\/][^"\'\r\n:]*?router_gate_hook\.py|/[^"\'\r\n:]*?router_gate_hook\.py|'
                              r'[A-Za-z]:[\\/][^"\'\r\n:]*?agentcompass\.exe(?=["\']?\s+gate-hook))')
    for hf in hook_files:
        if not hf.is_file():
            continue
        try:
            content = hf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        normalized = content.replace('\\"', '"').replace("\\\\", "\\")
        for candidate_text in (normalized, content):
            for match in hook_pattern.findall(candidate_text):
                hook_path = Path(match.strip())
                if hook_path.is_file():
                    info = _install_info_from_path(hook_path)
                    if info:
                        routers["hooks"] = info
                        break
            if "hooks" in routers:
                break
        if "hooks" in routers and (
            routers["hooks"]["version"] != tray["version"]
            or _norm_root(routers["hooks"]["root"]) != _norm_root(tray["root"])
        ):
            break

    # 3. agentcompass on PATH
    path_info = _resolve_cli_on_path(which_path=which_path)
    if path_info:
        routers["path"] = path_info

    if tray["shape"] == "wheel" and tray["version"] == "0.0.0" and "path" in routers:
        tray = dict(routers["path"])

    tray_norm = _norm_root(tray["root"])
    separate_routers: list[dict] = []
    seen_separate: set[tuple[str, str]] = set()
    for source, r_info in routers.items():
        r_norm = _norm_root(r_info["root"])
        if r_norm != tray_norm or r_info["version"] != tray["version"]:
            key = (r_norm, r_info["version"])
            if key not in seen_separate:
                seen_separate.add(key)
                separate_routers.append({**r_info, "source": source})

    wheel_installs: list[dict] = []
    seen_wheels: set[str] = set()
    for candidate in (tray, *routers.values()):
        if candidate.get("shape") == "wheel":
            w_norm = _norm_root(candidate["root"])
            if w_norm not in seen_wheels:
                seen_wheels.add(w_norm)
                wheel_installs.append(candidate)

    version_mismatch = any(r["version"] != tray["version"] for r in routers.values())
    if separate_routers or version_mismatch:
        mismatched = [r["version"] for r in separate_routers if r["version"] != tray["version"]]
        router_vers = "/".join(dict.fromkeys(mismatched or [r["version"] for r in separate_routers]))
        if version_mismatch:
            display_version = _tr("AgentCompass {0} · 命令行 {1}（版本不一致）").format(tray["version"], router_vers)
        else:
            display_version = _tr("AgentCompass {0} · 命令行 {1}").format(tray["version"], router_vers)
    else:
        display_version = f"AgentCompass {tray['version']}"

    return {
        "tray": tray,
        "routers": routers,
        "separate_routers": separate_routers,
        "wheel_installs": wheel_installs,
        "version_mismatch": version_mismatch,
        "display_version": display_version,
    }


def _release_cache_path(state_dir: Path | None = None) -> Path:
    return (Path(state_dir) if state_dir is not None else services.STATE_DIR) / RELEASE_CACHE_NAME


def _read_cached_release(cache_file: Path) -> dict | None:
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_cached_release(cache_file: Path, payload: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_file)
    except Exception:
        pass


def _parse_github_release(payload: dict, fallback_page: str) -> dict:
    tag = str(payload.get("tag_name") or "").strip()
    version_str = tag.lstrip("vV")
    assets = []
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        assets.append({
            "name": str(item.get("name") or ""),
            "url": str(item.get("browser_download_url") or item.get("url") or ""),
            "digest": str(item.get("digest") or ""),
            "size": int(item.get("size") or 0),
        })
    return {
        "checked_at": time.time(),
        "tag_name": f"v{version_str}" if version_str else "",
        "version": version_str,
        "html_url": str(payload.get("html_url") or fallback_page),
        "assets": assets,
    }


def check_product_release(
    force: bool = False,
    root: Path | None = None,
    state_dir: Path | None = None,
    opener=None,
    home: Path | None = None,
    which_path: str | Path | bool | None = False,
    frozen: bool | None = None,
    network: bool = True,
) -> dict:
    """Check GitHub Releases (cached for 6 h under state_dir) and combine with local install versions."""
    repo_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.append(str(repo_root))
    from agentcompass.origin import RELEASES_API, RELEASES_PAGE

    installs = inspect_installs(root=root, home=home, which_path=which_path, frozen=frozen)
    cache_file = _release_cache_path(state_dir)
    cached = _read_cached_release(cache_file)
    now = time.time()
    cache_fresh = bool(
        cached
        and cached.get("tag_name")
        and (now - float(cached.get("checked_at") or 0)) < RELEASE_CHECK_INTERVAL_SECONDS
    )

    release_data = cached or {}
    check_ok = True
    error_text = ""

    if network and (force or opener is not None or not cache_fresh):
        request = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "agentcompass"},
        )
        open_url = opener or urllib.request.urlopen
        try:
            with open_url(request, timeout=20) as response:
                raw_payload = json.loads(response.read().decode("utf-8"))
            release_data = _parse_github_release(raw_payload, RELEASES_PAGE)
            if state_dir is not None or opener is None:
                _write_cached_release(cache_file, release_data)
        except Exception as exc:
            check_ok = False
            error_text = str(exc)

    tray_ver = installs["tray"]["version"]
    remote_ver = str(release_data.get("version") or str(release_data.get("tag_name") or "").lstrip("vV")).strip()
    remote_tag = f"v{remote_ver}" if remote_ver else ""
    remote_key = _version_key(remote_ver) if remote_ver else (0,)
    tray_key = _version_key(tray_ver)
    router_keys = [_version_key(r["version"]) for r in installs["routers"].values() if r.get("version")]

    update_available = bool(remote_ver) and (
        remote_key > tray_key or (remote_key == tray_key and any(remote_key > rk for rk in router_keys))
    )
    update_label = _tr("可更新到 {0}").format(remote_tag) if update_available else ""
    menu_label = f"{installs['display_version']} · {update_label}" if update_available else installs["display_version"]

    if not check_ok:
        status_line = _tr("检查更新失败：{0}").format(error_text)
    elif update_available:
        status_line = update_label
    elif installs["version_mismatch"]:
        status_line = installs["display_version"]
    elif remote_tag:
        status_line = _tr("已是最新版本（{0}）").format(remote_tag)
    else:
        status_line = installs["display_version"]

    return {
        "ok": check_ok,
        "error": error_text,
        "tray_version": tray_ver,
        "shape": installs["tray"]["shape"],
        "installs": installs,
        "display_version": installs["display_version"],
        "version_mismatch": installs["version_mismatch"],
        "release": release_data,
        "remote_tag": remote_tag,
        "remote_version": remote_ver,
        "update_available": update_available,
        "update_label": update_label,
        "menu_label": menu_label,
        "status_line": status_line,
    }


def version_status(
    force: bool = False,
    network: bool = False,
    root: Path | None = None,
    state_dir: Path | None = None,
    opener=None,
    home: Path | None = None,
    which_path: str | Path | bool | None = False,
    frozen: bool | None = None,
) -> dict:
    return check_product_release(
        force=force,
        root=root,
        state_dir=state_dir,
        opener=opener,
        home=home,
        which_path=which_path,
        frozen=frozen,
        network=network or force or (opener is not None),
    )


def _find_release_asset(release: dict, predicate) -> dict | None:
    for asset in (release or {}).get("assets") or ():
        if isinstance(asset, dict) and predicate(str(asset.get("name") or "")):
            return asset
    return None


APP_MANIFEST_FILENAME = "app-manifest.json"
RUNTIME_INFO_FILENAME = "runtime-info.json"
RELEASE_MANIFEST_FILENAME = "manifest.json"
APP_REQUIRED_FILES = (
    "agentcompass/__init__.py",
    "agentcompass/cli.py",
    "tray/app.pyw",
    "tray/component_launch.py",
    "skills/agent-compass/SKILL.md",
    "setup/onboard.py",
)


class _IncompatibleCodePackageRuntime(RuntimeError):
    """Raised when an app-<version>.zip requires a runtime that does not match the installed runtime."""


def _verify_digest_string(digest: str, fallback_sha256: str = "") -> str:
    text = str(digest or "").strip()
    fallback = str(fallback_sha256 or "").strip().lower()
    if fallback.startswith("sha256:"):
        fallback = fallback.split(":", 1)[1].strip()
    if not text and fallback:
        text = f"sha256:{fallback}"
    if not text or not text.lower().startswith("sha256:"):
        raise RuntimeError(_tr("安装包缺少 sha256 校验值，已拒绝安装"))
    expected = text.split(":", 1)[1].strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeError(_tr("安装包 sha256 校验值格式无效，已拒绝安装"))
    if fallback:
        if len(fallback) != 64 or any(c not in "0123456789abcdef" for c in fallback):
            raise RuntimeError(_tr("安装包 sha256 校验值格式无效，已拒绝安装"))
        if expected != fallback:
            raise RuntimeError(
                _tr("安装包 sha256 校验不匹配（期望 {0}，实际 {1}），已拒绝安装").format(fallback[:12], expected[:12])
            )
    return expected


def _local_file_from_url(url: str) -> Path | None:
    if not url:
        return None
    if url.startswith("file://"):
        parsed = urllib.parse.urlparse(url)
        raw_path = urllib.request.url2pathname(parsed.path)
        cand = Path(raw_path)
        if cand.is_file():
            return cand
    cand = Path(url)
    return cand if cand.is_file() else None


def _download_verified_installer(
    asset: dict,
    dest_dir: Path,
    opener=None,
    *,
    fallback_sha256: str = "",
) -> Path:
    expected = _verify_digest_string(asset.get("digest") or "", fallback_sha256=fallback_sha256)
    url = str(asset.get("url") or asset.get("browser_download_url") or "").strip()
    if not url:
        raise RuntimeError(_tr("安装包缺少下载地址，已拒绝安装"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = str(asset.get("name") or "AgentCompass-tray-setup.exe")
    part_path = dest_dir / f"{name}.{uuid.uuid4().hex[:8]}.part"
    final_path = dest_dir / name
    local_src = _local_file_from_url(url)
    if local_src is not None:
        data = local_src.read_bytes()
    else:
        open_url = opener or urllib.request.urlopen
        request = urllib.request.Request(url, headers={"User-Agent": "agentcompass"})
        with open_url(request, timeout=300) as response:
            data = response.read()
    actual = hashlib.sha256(data).hexdigest().lower()
    if actual != expected:
        part_path.unlink(missing_ok=True)
        raise RuntimeError(
            _tr("安装包 sha256 校验不匹配（期望 {0}，实际 {1}），已拒绝安装").format(expected[:12], actual[:12])
        )
    part_path.write_bytes(data)
    part_path.replace(final_path)
    return final_path


def read_local_runtime_info(root: Path) -> dict | None:
    """Read <root>/runtime-info.json (or fallback runtime_fingerprint from <root>/manifest.json)."""
    root = Path(root)
    rt_file = root / RUNTIME_INFO_FILENAME
    if rt_file.is_file():
        try:
            payload = json.loads(rt_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, dict) and str(payload.get("runtime_fingerprint") or "").strip():
                return payload
        except Exception:
            pass
    manifest_file = root / RELEASE_MANIFEST_FILENAME
    if manifest_file.is_file():
        try:
            payload = json.loads(manifest_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, dict) and str(payload.get("runtime_fingerprint") or "").strip():
                return {
                    "runtime_fingerprint": str(payload.get("runtime_fingerprint")).strip(),
                    "runtime_version": str(payload.get("version") or "0.0.0").strip(),
                }
        except Exception:
            pass
    return None


def read_local_runtime_fingerprint(root: Path) -> str | None:
    info = read_local_runtime_info(root)
    if not isinstance(info, dict):
        return None
    fp = str(info.get("runtime_fingerprint") or "").strip()
    return fp or None


def _load_release_manifest(release: dict, opener=None) -> dict | None:
    if not isinstance(release, dict):
        return None
    if isinstance(release.get("manifest"), dict):
        return {**release["manifest"], "_digest_verified": True}
    asset = _find_release_asset(release, lambda name: name == RELEASE_MANIFEST_FILENAME)
    if asset is None:
        return None
    url = str(asset.get("url") or asset.get("browser_download_url") or "").strip()
    if not url:
        return None
    try:
        local_src = _local_file_from_url(url)
        if local_src is not None:
            data = local_src.read_bytes()
        else:
            open_url = opener or urllib.request.urlopen
            request = urllib.request.Request(url, headers={"User-Agent": "agentcompass"})
            with open_url(request, timeout=60) as response:
                data = response.read()
    except Exception:
        return None
    digest = str(asset.get("digest") or "").strip()
    if digest:
        expected = _verify_digest_string(digest)
        actual = hashlib.sha256(data).hexdigest().lower()
        if actual != expected:
            raise RuntimeError(
                _tr("manifest.json sha256 校验不匹配（期望 {0}，实际 {1}），已拒绝采信").format(
                    expected[:12], actual[:12]
                )
            )
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return {**payload, "_digest_verified": bool(digest)}


def can_use_code_package(local_runtime: dict | None, manifest: dict | None) -> tuple[bool, str]:
    """Determine whether an installer install can update via app-<version>.zip instead of the full setup.exe."""
    if not isinstance(local_runtime, dict):
        return False, "missing_local_runtime_info"
    local_fp = str(local_runtime.get("runtime_fingerprint") or "").strip().lower()
    if not local_fp:
        return False, "missing_local_runtime_fingerprint"
    if not isinstance(manifest, dict):
        return False, "missing_release_manifest"
    manifest_fp = str(manifest.get("runtime_fingerprint") or "").strip().lower()
    app_pkg = manifest.get("app_package")
    if not isinstance(app_pkg, dict):
        return False, "missing_app_package_in_manifest"
    pkg_name = str(app_pkg.get("name") or "").strip()
    pkg_sha = str(app_pkg.get("sha256") or "").strip().lower()
    if not pkg_name or not pkg_sha:
        return False, "incomplete_app_package_entry"
    required_fp = str(app_pkg.get("required_runtime_fingerprint") or manifest_fp or "").strip().lower()
    if not required_fp:
        return False, "missing_required_runtime_fingerprint"
    min_rt_version = str(app_pkg.get("min_runtime_version") or manifest.get("min_runtime_version") or "").strip()
    local_rt_version = str(
        local_runtime.get("runtime_version") or local_runtime.get("version") or "0.0.0"
    ).strip()
    if min_rt_version and local_rt_version and local_rt_version != "0.0.0":
        if _version_key(local_rt_version) < _version_key(min_rt_version):
            return False, f"runtime_version_too_old:{local_rt_version}<{min_rt_version}"
    local_iss = str(local_runtime.get("iss_sha256") or "").strip().lower()
    manifest_rt = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    manifest_iss = str(manifest.get("iss_sha256") or manifest_rt.get("iss_sha256") or "").strip().lower()
    if local_iss and manifest_iss and local_iss != manifest_iss:
        return False, f"iss_sha256_mismatch:{local_iss[:12]}!={manifest_iss[:12]}"
    if manifest_fp and local_fp != manifest_fp:
        return False, f"runtime_fingerprint_mismatch:{local_fp[:12]}!={manifest_fp[:12]}"
    if local_fp != required_fp:
        return False, f"required_runtime_fingerprint_mismatch:{local_fp[:12]}!={required_fp[:12]}"
    return True, "runtime_fingerprint_match"


def _remove_dir_or_link(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        path.unlink()
        return
    except OSError:
        pass
    if os.name == "nt":
        try:
            os.rmdir(path)
            return
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def _ensure_legacy_cli_data_links(install_root: Path, active_dir: Path) -> None:
    """Keep <install_root>/cli/_internal/agentcompass/data/{skills,setup} pointing to <active_dir>/{skills,setup}."""
    cli_internal = install_root / "cli" / "_internal"
    if not cli_internal.is_dir():
        return
    data_root = cli_internal / "agentcompass" / "data"
    try:
        data_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in ("skills", "setup"):
        target = active_dir / name
        if not target.is_dir():
            continue
        link = data_root / name
        try:
            if link.exists() and link.resolve() == target.resolve():
                continue
        except OSError:
            pass
        _remove_dir_or_link(link)
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=False,
                capture_output=True,
                creationflags=services.CREATE_NO_WINDOW,
            )
        if not link.exists():
            try:
                os.symlink(str(target), str(link), target_is_directory=True)
            except OSError:
                shutil.copytree(target, link, dirs_exist_ok=True)


def _validate_extracted_app_dir(
    app_dir: Path,
    *,
    expected_runtime_fingerprint: str = "",
    local_runtime_version: str = "",
) -> dict:
    for rel in APP_REQUIRED_FILES:
        if not (app_dir / rel).is_file():
            raise RuntimeError(_tr("代码包缺少必要文件：{0}").format(rel))
    manifest_file = app_dir / APP_MANIFEST_FILENAME
    if not manifest_file.is_file():
        raise RuntimeError(_tr("代码包缺少清单文件 app-manifest.json"))
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(_tr("代码包清单 app-manifest.json 格式无效：{0}").format(exc)) from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(_tr("代码包清单 app-manifest.json 格式无效"))

    expected_fp = str(expected_runtime_fingerprint or "").strip().lower()
    required_fp = str(manifest.get("required_runtime_fingerprint") or "").strip().lower()
    if expected_fp and required_fp and required_fp != expected_fp:
        raise _IncompatibleCodePackageRuntime(
            _tr("代码包要求的运行时指纹不匹配（本地 {0}，要求 {1}）").format(expected_fp[:12], required_fp[:12])
        )
    min_rt = str(manifest.get("min_runtime_version") or "").strip()
    local_rt_ver = str(local_runtime_version or "").strip()
    if min_rt and local_rt_ver and local_rt_ver != "0.0.0":
        if _version_key(local_rt_ver) < _version_key(min_rt):
            raise _IncompatibleCodePackageRuntime(
                _tr("代码包要求的最低运行时版本（{0}）高于当前运行时版本（{1}）").format(min_rt, local_rt_ver)
            )

    files_map = manifest.get("files")
    if isinstance(files_map, dict):
        for rel, info in files_map.items():
            if isinstance(info, dict):
                exp_sha = str(info.get("sha256") or "").strip().lower()
            elif isinstance(info, str):
                exp_sha = info.strip().lower()
            else:
                continue
            if not exp_sha:
                continue
            fpath = app_dir / str(rel)
            if not fpath.is_file():
                raise RuntimeError(_tr("代码包缺少清单所列文件：{0}").format(rel))
            act_sha = hashlib.sha256(fpath.read_bytes()).hexdigest().lower()
            if act_sha != exp_sha:
                raise RuntimeError(_tr("代码包文件校验失败：{0}").format(rel))
    return manifest


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (dest_dir / member.filename).resolve()
            if dest_resolved != member_path and dest_resolved not in member_path.parents:
                raise RuntimeError(_tr("代码包包含非法路径：{0}").format(member.filename))
        zf.extractall(dest_dir)


def _replace_or_copy_file(tmp_target: Path, target: Path) -> None:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            tmp_target.replace(target)
            return
        except PermissionError as exc:
            last_err = exc
            try:
                shutil.copy2(tmp_target, target)
                tmp_target.unlink(missing_ok=True)
                return
            except OSError as copy_exc:
                last_err = copy_exc
                if attempt < 2:
                    time.sleep(0.05)
    tmp_target.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err


def _sync_dir_in_place(src_dir: Path, dest_dir: Path) -> None:
    """Synchronize dest_dir to match src_dir in-place when Windows holds a directory handle on dest_dir."""
    src_dir = Path(src_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_rel_paths: set[Path] = set()
    for item in src_dir.rglob("*"):
        rel = item.relative_to(src_dir)
        src_rel_paths.add(rel)
        target = dest_dir / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
            shutil.copy2(item, tmp_target)
            _replace_or_copy_file(tmp_target, target)
    for existing in sorted(dest_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = existing.relative_to(dest_dir)
        if rel not in src_rel_paths:
            if existing.is_file() or existing.is_symlink():
                try:
                    existing.unlink()
                except OSError:
                    pass
            elif existing.is_dir():
                try:
                    existing.rmdir()
                except OSError:
                    shutil.rmtree(existing, ignore_errors=True)


def rollback_code_package(root: Path, reason: str = "") -> Path | None:
    """Roll back <root>/app/current to <root>/app/previous and record rollback state."""
    root = Path(root)
    app_root = root / "app"
    current_dir = app_root / "current"
    previous_dir = app_root / "previous"
    if not previous_dir.is_dir():
        return None
    prev_manifest = _validate_extracted_app_dir(previous_dir)
    broken_dir = app_root / f"broken-{int(time.time())}"
    shutil.rmtree(broken_dir, ignore_errors=True)
    try:
        if current_dir.exists():
            current_dir.replace(broken_dir)
        previous_dir.replace(current_dir)
        shutil.rmtree(broken_dir, ignore_errors=True)
    except PermissionError:
        shutil.rmtree(broken_dir, ignore_errors=True)
        _sync_dir_in_place(previous_dir, current_dir)
        shutil.rmtree(previous_dir, ignore_errors=True)
    _ensure_legacy_cli_data_links(root, current_dir)
    state_file = app_root / "state.json"
    state: dict = {}
    if state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}
    restored_ver = str(prev_manifest.get("version") or local_version(current_dir))
    state["current_version"] = restored_ver
    state["active_version"] = restored_ver
    state["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["rollback_reason"] = reason
    try:
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass
    return current_dir


def _apply_code_package(
    root: Path,
    zip_path: Path,
    *,
    expected_runtime_fingerprint: str = "",
    local_runtime_version: str = "",
) -> dict:
    """Atomically unpack and activate app-<version>.zip under <root>/app/current, retaining <root>/app/previous."""
    root = Path(root)
    app_root = root / "app"
    current_dir = app_root / "current"
    previous_dir = app_root / "previous"
    packages_dir = app_root / "packages"
    staging_dir = app_root / f"staging-{uuid.uuid4().hex[:8]}"
    backup_prev = app_root / f"previous-backup-{uuid.uuid4().hex[:8]}"
    app_root.mkdir(parents=True, exist_ok=True)
    packages_dir.mkdir(parents=True, exist_ok=True)

    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(zip_path, staging_dir)
        new_manifest = _validate_extracted_app_dir(
            staging_dir,
            expected_runtime_fingerprint=expected_runtime_fingerprint,
            local_runtime_version=local_runtime_version,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    old_version = local_version(current_dir) if current_dir.is_dir() else None
    new_version = str(new_manifest.get("version") or local_version(staging_dir)).strip().lstrip("vV")
    moved_current_to_previous = False
    moved_previous_to_backup = False

    try:
        if previous_dir.exists():
            shutil.rmtree(backup_prev, ignore_errors=True)
            try:
                previous_dir.replace(backup_prev)
            except PermissionError:
                shutil.copytree(previous_dir, backup_prev, dirs_exist_ok=True)
                shutil.rmtree(previous_dir, ignore_errors=True)
            moved_previous_to_backup = True
        if current_dir.exists():
            try:
                current_dir.replace(previous_dir)
                moved_current_to_previous = True
                staging_dir.replace(current_dir)
            except PermissionError:
                # A live child process on Windows holds an open directory handle inside current_dir.
                # Save current_dir to previous_dir and synchronize staging_dir into current_dir in-place.
                if not previous_dir.exists():
                    shutil.copytree(current_dir, previous_dir, dirs_exist_ok=True)
                moved_current_to_previous = True
                _sync_dir_in_place(staging_dir, current_dir)
                shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            staging_dir.replace(current_dir)
        # Re-validate after activation so any failure triggers rollback to previous_dir.
        _validate_extracted_app_dir(
            current_dir,
            expected_runtime_fingerprint=expected_runtime_fingerprint,
            local_runtime_version=local_runtime_version,
        )
        pkg_target = packages_dir / f"app-{new_version}.zip"
        if zip_path.resolve() != pkg_target.resolve():
            shutil.copy2(zip_path, pkg_target)
        _ensure_legacy_cli_data_links(root, current_dir)
        state_payload = {
            "current_version": new_version,
            "active_version": new_version,
            "previous_version": old_version if (moved_current_to_previous and old_version != "0.0.0") else None,
            "runtime_fingerprint": expected_runtime_fingerprint or str(new_manifest.get("required_runtime_fingerprint") or ""),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (app_root / "state.json").write_text(
            json.dumps(state_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        root_manifest_file = root / RELEASE_MANIFEST_FILENAME
        if root_manifest_file.is_file():
            try:
                rm_data = json.loads(root_manifest_file.read_text(encoding="utf-8"))
                if isinstance(rm_data, dict):
                    rm_data["version"] = new_version
                    root_manifest_file.write_text(
                        json.dumps(rm_data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
            except Exception:
                pass
        if moved_previous_to_backup and backup_prev.exists():
            shutil.rmtree(backup_prev, ignore_errors=True)
        return new_manifest
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if moved_current_to_previous and previous_dir.exists():
            try:
                shutil.rmtree(current_dir, ignore_errors=True)
                if current_dir.exists():
                    _sync_dir_in_place(previous_dir, current_dir)
                    shutil.rmtree(previous_dir, ignore_errors=True)
                else:
                    previous_dir.replace(current_dir)
                _ensure_legacy_cli_data_links(root, current_dir)
            except OSError:
                try:
                    _sync_dir_in_place(previous_dir, current_dir)
                    _ensure_legacy_cli_data_links(root, current_dir)
                except OSError:
                    pass
        if moved_previous_to_backup and backup_prev.exists() and not previous_dir.exists():
            try:
                backup_prev.replace(previous_dir)
            except OSError:
                pass
        shutil.rmtree(backup_prev, ignore_errors=True)
        raise


def _uv_executable() -> str:
    try:
        uv = services.uv_path()
        if uv.is_file():
            return str(uv)
    except Exception:
        pass
    return shutil.which("uv") or "uv"


def _upgrade_wheel_install(
    wheel_info: dict,
    release: dict,
    exec_run,
    opener=None,
) -> str:
    wheel_asset = _find_release_asset(
        release, lambda name: name.startswith("agentcompass-") and name.endswith(".whl")
    )
    wheel_url = str((wheel_asset or {}).get("url") or (wheel_asset or {}).get("browser_download_url") or "").strip()
    local_wheel = _local_file_from_url(wheel_url)
    if wheel_asset is not None and "digest" in wheel_asset and wheel_asset.get("digest") is not None:
        expected_sha = _verify_digest_string(str(wheel_asset.get("digest") or ""))
        if local_wheel is not None:
            actual_sha = hashlib.sha256(local_wheel.read_bytes()).hexdigest().lower()
            if actual_sha != expected_sha:
                raise RuntimeError(
                    _tr("安装包 sha256 校验不匹配（期望 {0}，实际 {1}），已拒绝安装").format(
                        expected_sha[:12], actual_sha[:12]
                    )
                )
        elif opener is not None and wheel_url:
            request = urllib.request.Request(wheel_url, headers={"User-Agent": "agentcompass"})
            with opener(request, timeout=120) as response:
                data = response.read()
            actual_sha = hashlib.sha256(data).hexdigest().lower()
            if actual_sha != expected_sha:
                raise RuntimeError(
                    _tr("安装包 sha256 校验不匹配（期望 {0}，实际 {1}），已拒绝安装").format(
                        expected_sha[:12], actual_sha[:12]
                    )
                )

    wheel_target = str(local_wheel) if (local_wheel is not None and wheel_url.startswith("file://")) else wheel_url
    installer_tool = str(wheel_info.get("installer") or "pip").strip().lower()
    # Never run the installer from inside the install it replaces: Windows can't remove a
    # directory that is a live process's cwd (uv tool install --force fails with os error 32).
    cwd = Path(tempfile.gettempdir())
    tag_label = str((release or {}).get("tag_name") or (Path(wheel_target).name if wheel_target else "latest"))
    if installer_tool == "uv":
        uv_bin = _uv_executable()
        if wheel_target:
            cmd = [uv_bin, "tool", "install", "--force", wheel_target]
        else:
            cmd = [uv_bin, "tool", "upgrade", "agentcompass"]
        exec_run(cmd, cwd)
        return _tr("已通过 uv 更新 Router wheel（{0}）").format(tag_label)

    if not wheel_target:
        raise RuntimeError(_tr("发布版本中未找到 wheel 安装包，无法通过 pip 更新"))
    py_bin = str(wheel_info.get("python") or sys.executable)
    cmd = [py_bin, "-m", "pip", "install", "--upgrade", wheel_target]
    exec_run(cmd, cwd)
    return _tr("已通过 pip 更新 Router wheel（{0}）").format(tag_label)


def _update_git_checkout(root: Path, exec_run) -> tuple[str, bool]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    dirty = exec_run(["git", "status", "--porcelain", "--untracked-files=no"], root, env=env)
    if dirty.strip():
        return _tr("AgentCompass 仓库有本地改动，未执行 git 更新"), False
    branch = exec_run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, env=env).strip()
    if not branch or branch == "HEAD":
        return _tr("AgentCompass 仓库处于游离 HEAD 状态，未执行 git 更新"), False
    try:
        upstream = exec_run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            root,
            env=env,
        ).strip()
    except Exception:
        upstream = ""
    if not (upstream == "origin" or upstream.startswith("origin/")):
        return _tr("AgentCompass 当前分支 {0} 未跟踪 origin，未执行 git 更新").format(branch), False
    exec_run(["git", "fetch", "origin"], root, env=env)
    exec_run(["git", "pull", "--ff-only"], root, env=env)
    return _tr("AgentCompass 仓库已更新（git pull --ff-only）"), True


def _refresh_public_data_step(installs: dict, exec_run) -> str:
    tray_root = Path(installs["tray"]["root"])
    path_router = installs["routers"].get("path") or {}
    cli_bin = path_router.get("cli") or shutil.which("agentcompass")
    bundled_cli = tray_root / "cli" / ("agentcompass.exe" if os.name == "nt" else "agentcompass")
    if cli_bin:
        cmd = [str(cli_bin), "data", "--refresh"]
    elif bundled_cli.is_file():
        cmd = [str(bundled_cli), "data", "--refresh"]
    elif installs["wheel_installs"] and installs["wheel_installs"][0].get("python"):
        cmd = [str(installs["wheel_installs"][0]["python"]), "-m", "agentcompass", "data", "--refresh"]
    else:
        cmd = [sys.executable, "-m", "agentcompass", "data", "--refresh"]
    exec_run(cmd, tray_root)
    return _tr("已刷新公共路由数据（agentcompass data --refresh）")


def _restart_tray_step(state_dir: Path | None = None) -> str:
    try:
        import deploy
        if not deploy.tray_processes():
            deploy.stop_resident_components(restart_usage_float=True)
            return _tr("未检测到运行中的托盘，无需重启托盘")
        deploy.write_restart_marker(version=local_version(), state_dir=state_dir)
        deploy.stop_resident_components()
    except Exception:
        pass
    subprocess.Popen(
        component_launch.command("deploy", "restart", "--refresh-components"),
        cwd=str(services.safe_cwd()),
        creationflags=services.CREATE_NO_WINDOW | services.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return _tr("正在重启托盘…")


def update_agentcompass(
    progress=None,
    *,
    on_event=None,
    root: Path | None = None,
    home: Path | None = None,
    state_dir: Path | None = None,
    which_path: str | Path | bool | None = False,
    frozen: bool | None = None,
    opener=None,
    release: dict | None = None,
    runner=None,
    restart_fn=None,
) -> str:
    """One-click update for AgentCompass across installer, wheel, and git checkout shapes."""
    if not LOCK.acquire(blocking=False):
        return _tr("正在更新，请稍候")
    try:
        user_cb = on_event if on_event is not None else progress
        if state_dir is not None:
            import update_status

            def _dispatch_event(ev):
                update_status.record(ev, directory=Path(state_dir))
                if user_cb is not None:
                    user_cb(ev)

            _progress.callback = _dispatch_event
        else:
            _progress.callback = user_cb
        _progress.component = "AgentCompass"
        report("start", _tr("开始检查并更新 AgentCompass"), "", title="AgentCompass", targets=["AgentCompass"])

        def exec_run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
            if runner is not None:
                report("progress", _tr("执行 {0} {1}").format(Path(args[0]).name, " ".join(args[1:3])))
                out = runner(args, cwd=cwd, env=env)
                return "" if out is None else str(out)
            return run(args, cwd, env=env)

        installs = inspect_installs(root=root, home=home, which_path=which_path, frozen=frozen)
        tray = installs["tray"]
        shape = tray["shape"]
        need_release = shape in ("installer", "wheel") or bool(installs["wheel_installs"])
        release_info = release
        if release_info is None and need_release:
            checked = check_product_release(
                force=True,
                root=root,
                state_dir=state_dir,
                opener=opener,
                home=home,
                which_path=which_path,
                frozen=frozen,
                network=True,
            )
            release_info = checked.get("release") or {}

        steps: list[str] = []
        updated_any = False

        if shape == "installer":
            inst_root = Path(tray["root"])
            dl_dir = (Path(state_dir) if state_dir is not None else services.STATE_DIR) / "downloads"
            local_rt = read_local_runtime_info(inst_root)
            manifest_data = _load_release_manifest(release_info or {}, opener=opener)
            use_code_pkg, pkg_reason = can_use_code_package(local_rt, manifest_data)
            app_pkg_info = (manifest_data or {}).get("app_package") if isinstance(manifest_data, dict) else None
            app_asset_name = str((app_pkg_info or {}).get("name") or "")
            app_asset = (
                _find_release_asset(
                    release_info or {},
                    lambda name: (bool(app_asset_name) and name == app_asset_name)
                    or (name.startswith("app-") and name.endswith(".zip")),
                )
                if use_code_pkg
                else None
            )
            if not use_code_pkg:
                reason_text = pkg_reason or "incompatible_runtime"
                report(
                    "progress",
                    _tr("运行时已变更，本次更新需要完整安装包（{0}）").format(reason_text),
                    reason=reason_text,
                    full_installer_reason=reason_text,
                )
            elif app_asset is None:
                reason_text = "missing_app_package_asset"
                report(
                    "progress",
                    _tr("运行时已变更，本次更新需要完整安装包（{0}）").format(reason_text),
                    reason=reason_text,
                    full_installer_reason=reason_text,
                )
            used_code_pkg = False
            manifest_verified = bool((manifest_data or {}).get("_digest_verified"))
            if use_code_pkg and app_asset is not None:
                try:
                    report("component", _tr("下载并校验代码增量包"))
                    manifest_sha = (
                        str((app_pkg_info or {}).get("sha256") or "").strip().lower()
                        if manifest_verified
                        else ""
                    )
                    app_zip = _download_verified_installer(
                        app_asset,
                        dl_dir,
                        opener=opener,
                        fallback_sha256=manifest_sha,
                    )
                    applied_manifest = _apply_code_package(
                        inst_root,
                        app_zip,
                        expected_runtime_fingerprint=str((local_rt or {}).get("runtime_fingerprint") or ""),
                        local_runtime_version=str(
                            (local_rt or {}).get("runtime_version") or (local_rt or {}).get("version") or ""
                        ),
                    )
                    pkg_label = (
                        str((release_info or {}).get("tag_name") or "")
                        or f"v{applied_manifest.get('version')}"
                        if applied_manifest.get("version")
                        else app_zip.name
                    )
                    msg = _tr("已完成代码包增量更新（{0}）").format(pkg_label)
                    steps.append(msg)
                    report("result", msg)
                    updated_any = True
                    used_code_pkg = True
                except _IncompatibleCodePackageRuntime as exc:
                    used_code_pkg = False
                    reason_text = str(exc) or "incompatible_code_package_runtime"
                    report(
                        "progress",
                        _tr("运行时已变更，本次更新需要完整安装包（{0}）").format(reason_text),
                        reason=reason_text,
                        full_installer_reason=reason_text,
                    )

            if not used_code_pkg:
                report("component", _tr("下载并校验托盘安装包"))
                installer_info = (manifest_data or {}).get("installer") if isinstance(manifest_data, dict) else None
                installer_name = str((installer_info or {}).get("name") or "AgentCompass-tray-setup.exe")
                installer_sha = (
                    str((installer_info or {}).get("sha256") or "").strip().lower()
                    if manifest_verified
                    else ""
                )
                installer_asset = _find_release_asset(
                    release_info or {},
                    lambda name: name == installer_name
                    or name == "AgentCompass-tray-setup.exe"
                    or name.endswith("-setup.exe"),
                )
                if installer_asset is None:
                    raise RuntimeError(_tr("发布版本中未找到安装包 AgentCompass-tray-setup.exe"))
                installer_exe = _download_verified_installer(
                    installer_asset,
                    dl_dir,
                    opener=opener,
                    fallback_sha256=installer_sha,
                )
                is_live_frozen_self = (
                    runner is None
                    and bool(frozen if frozen is not None else component_launch.frozen())
                    and Path(sys.executable).name.lower() == "agentcompass.exe"
                )
                if is_live_frozen_self:
                    # Inno Setup PrepareToInstall terminates AgentCompass.exe (`taskkill /F /IM AgentCompass.exe`)
                    # and [Run] launches the new AgentCompass.exe at the end of installation. Update any separate
                    # wheels first, then record pending_installer before launching setup.exe so update-status.json
                    # stays running=True until the installer actually finishes and reports its real exit code.
                    for wheel_info in installs["wheel_installs"]:
                        report("component", _tr("更新独立 Router wheel"))
                        w_msg = _upgrade_wheel_install(wheel_info, release_info or {}, exec_run=exec_run, opener=opener)
                        steps.append(w_msg)
                        report("result", w_msg)
                    target_ver = str(
                        (release_info or {}).get("version")
                        or (release_info or {}).get("tag_name")
                        or ""
                    ).strip().lstrip("vV")
                    tag_label = str(
                        (release_info or {}).get("tag_name")
                        or (f"v{target_ver}" if target_ver else installer_exe.name)
                    )
                    try:
                        import deploy
                        deploy.write_restart_marker(version=target_ver or local_version(), state_dir=state_dir)
                        deploy.stop_resident_components()
                    except Exception:
                        pass
                    started_at = time.time()
                    report(
                        "progress",
                        _tr("正在运行安装包 {0}").format(installer_exe.name),
                        pending_installer={
                            "target_version": target_ver,
                            "started_at": started_at,
                            "tag_label": tag_label,
                            "install_root": str(inst_root),
                        },
                    )
                    proc = subprocess.Popen(
                        [str(installer_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                        cwd=str(installer_exe.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=services.CREATE_NO_WINDOW | services.CREATE_NEW_PROCESS_GROUP,
                        close_fds=True,
                    )
                    proc_pid = getattr(proc, "pid", None)
                    if isinstance(proc_pid, int) and proc_pid > 0:
                        report(
                            "progress",
                            _tr("正在运行安装包 {0}").format(installer_exe.name),
                            pending_installer={
                                "pid": proc_pid,
                                "target_version": target_ver,
                                "started_at": started_at,
                                "tag_label": tag_label,
                                "install_root": str(inst_root),
                            },
                        )
                    wait_fn = getattr(proc, "wait", None)
                    wait_rc = wait_fn() if callable(wait_fn) else 0
                    rc = getattr(proc, "returncode", None)
                    if not isinstance(rc, int):
                        rc = wait_rc if isinstance(wait_rc, int) else 0
                    if rc != 0:
                        try:
                            import deploy
                            deploy.consume_restart_marker(state_dir=state_dir)
                        except Exception:
                            pass
                        raise RuntimeError(
                            _tr("{0} 失败（退出码 {1}）").format(installer_exe.name, rc)
                        )
                    report("component", _tr("刷新公共路由数据"))
                    refresh_msg = _refresh_public_data_step(installs, exec_run=exec_run)
                    steps.append(refresh_msg)
                    report("result", refresh_msg)
                    msg = _tr("已完成安装包静默更新（{0}）").format(tag_label)
                    steps.append(msg)
                    report("result", msg)
                    summary = "；".join(steps)
                    report("finish", _tr("AgentCompass 更新结束"), "", pending_installer=None)
                    return summary

                exec_run([str(installer_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], installer_exe.parent)
                msg = _tr("已完成安装包静默更新（{0}）").format((release_info or {}).get("tag_name") or installer_exe.name)
                steps.append(msg)
                report("result", msg)
                updated_any = True
            for wheel_info in installs["wheel_installs"]:
                report("component", _tr("更新独立 Router wheel"))
                w_msg = _upgrade_wheel_install(wheel_info, release_info or {}, exec_run=exec_run, opener=opener)
                steps.append(w_msg)
                report("result", w_msg)
        elif shape == "git":
            report("component", _tr("检查 Git 仓库工作区与分支"))
            git_msg, git_updated = _update_git_checkout(Path(tray["root"]), exec_run=exec_run)
            steps.append(git_msg)
            report("result", git_msg)
            if git_updated:
                updated_any = True
            for wheel_info in installs["wheel_installs"]:
                report("component", _tr("更新独立 Router wheel"))
                w_msg = _upgrade_wheel_install(wheel_info, release_info or {}, exec_run=exec_run, opener=opener)
                steps.append(w_msg)
                report("result", w_msg)
                updated_any = True
        else:
            report("component", _tr("更新 Router wheel"))
            targets = installs["wheel_installs"] or [tray]
            for wheel_info in targets:
                w_msg = _upgrade_wheel_install(wheel_info, release_info or {}, exec_run=exec_run, opener=opener)
                steps.append(w_msg)
                report("result", w_msg)
                updated_any = True

        if updated_any:
            report("component", _tr("刷新公共路由数据"))
            refresh_msg = _refresh_public_data_step(installs, exec_run=exec_run)
            steps.append(refresh_msg)
            report("result", refresh_msg)

            report("component", _tr("重启托盘"))
            restart_msg = restart_fn() if restart_fn is not None else _restart_tray_step(state_dir=state_dir)
            if restart_msg:
                steps.append(restart_msg)
                report("result", restart_msg)

        summary = "；".join(steps)
        report("finish", _tr("AgentCompass 更新结束"), "")
        return summary
    except Exception as error:
        err_msg = _tr("AgentCompass 更新失败：{0}").format(error)
        report("result", err_msg)
        report("finish", _tr("AgentCompass 更新结束"), "", pending_installer=None)
        raise
    finally:
        _progress.callback = None
        _progress.component = ""
        LOCK.release()


def release_notice(root: Path, opener=None) -> ProductNoticeResult:
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
        return ProductNoticeResult(
            f"AgentCompass {local} has no .git checkout, so it cannot git pull. "
            f"Could not read GitHub releases ({error}). "
            f"Upgrade with: uv tool upgrade agentcompass "
            f"or download the installer from {RELEASES_PAGE}",
            status="failed",
        )
    tag = str(payload.get("tag_name") or "")
    page = str(payload.get("html_url") or RELEASES_PAGE)
    remote = _version_key(tag)
    here = _version_key(local)
    if remote > here:
        return ProductNoticeResult(
            f"AgentCompass {local} is not a git checkout. GitHub release {tag} is newer. "
            f"Upgrade the router with: uv tool upgrade agentcompass "
            f"Or download the Windows installer from: {page}",
            status="newer",
        )
    if remote == here:
        return ProductNoticeResult(
            f"AgentCompass {local} matches GitHub release {tag}. No upgrade is waiting.",
            status="current",
        )
    return ProductNoticeResult(
        f"AgentCompass {local} is newer than GitHub release {tag or 'unknown'}.",
        status="ahead",
    )


def product_notice(root: Path | None = None, opener=None) -> ProductNoticeResult:
    """Empty for a dev checkout, so the existing git updater is unchanged."""
    root = root or Path(__file__).resolve().parents[1]
    if (root / ".git").exists():
        return ProductNoticeResult("", status="dev")
    try:
        return release_notice(root, opener=opener)
    except Exception as error:
        return ProductNoticeResult(
            f"AgentCompass has no git checkout and the release check failed: {error}. "
            "Upgrade with: uv tool upgrade agentcompass "
            "or download the installer from https://github.com/akiiwa-games/AgentCompass/releases/latest",
            status="failed",
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
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("check", "build", "update", "update-agentcompass"))
    parser.add_argument("--release-file", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--which-path", type=str, default=None)
    args = parser.parse_args()
    action = args.action
    if action == "build":
        build()
        print(_tr("已构建"))
    elif action == "update":
        from update_status import record
        print(update_all(progress=record))
    elif action == "update-agentcompass":
        from update_status import record
        release_data = None
        if args.release_file is not None:
            release_data = json.loads(args.release_file.read_text(encoding="utf-8-sig"))
        state_dir = args.state_dir
        progress_cb = (lambda ev: record(ev, directory=Path(state_dir))) if state_dir is not None else record
        which_arg: str | Path | bool | None = False
        if args.which_path is not None:
            which_arg = None if args.which_path == "" else Path(args.which_path)
        try:
            print(
                update_agentcompass(
                    progress=progress_cb,
                    home=args.home,
                    state_dir=state_dir,
                    which_path=which_arg,
                    release=release_data,
                ),
                flush=True,
            )
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1
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
