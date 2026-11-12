"""Local CLI versions, model-settings controls and daily background updates.

    snapshot()   what is installed, from where, and how it can be updated; local reads only, no network
    update_all() check and update supported installs; shared by the home toolbar and daily maintenance

The executable is the one the router launches: the command a provider's Paseo definition names (its sibling `grok`
when it names `agent`), else the tool on PATH (Grok Build also in ~/.grok/bin, which its installer leaves off PATH).
A bare name is resolved with the same env overlay `roll` applies at launch: the provider's Paseo `env` replaces the
process env, and a child's keys replace the provider it extends. `PATH` is replaced as a whole, never prepended.
The update is derived from that file, never from a guess: an npm-global shim updates its own npm package into its own
prefix (`npm install -g <package>@latest --prefix <prefix>`, the command each vendor documents for npm installs), and a
native install in the vendor's directory runs the vendor's own updater. Anything else (portable copies, other package
managers, a configured command that is not that tool, registered custom ACP launchers) is shown with a diagnostic and
stays routable. Credentials and user configuration are never read or written, and no agent or harness is restarted.
"""
from __future__ import annotations

from i18n import translate as _tr

import json
from contextlib import ExitStack
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import owned_process
import cli_update_files

CHECK_TIMEOUT = 60
VERSION_TIMEOUT = 20
UPDATE_TIMEOUT = 600
AUTO_UPDATE_INTERVAL_SECONDS = 24 * 3600
AUTO_UPDATE_STARTUP_SECONDS = 180
_started_at = time.monotonic()
_state_lock = threading.RLock()
_batch_lock = threading.Lock()
_auto_running = False
VERSION = re.compile(r"\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.]+)?")
DEFAULT_PROVIDERS = ("claude", "codex", "pi", "opencode")    # always launchable through Paseo, even without a config entry
_lock = threading.Lock()
_busy: dict[str, str] = {}
_latest: dict[str, str] = {}
_STDLIB_WHICH = shutil.which
_STDLIB_OWNED_RUN = owned_process.run
_WHICH_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_VERSION_CACHE: dict[tuple[str, int, int], str] = {}


def _which(command: str, path_value: str | None) -> str | None:
    if shutil.which is not _STDLIB_WHICH:
        return shutil.which(command, path=path_value)
    key = (command, path_value or "")
    now = time.monotonic()
    with _lock:
        cached = _WHICH_CACHE.get(key)
    if cached is not None and now - cached[0] < 15.0 and (cached[1] is None or os.path.isfile(cached[1])):
        return cached[1]
    found = shutil.which(command, path=path_value)
    with _lock:
        _WHICH_CACHE[key] = (now, found)
    return found


def _clear_which_cache() -> None:
    with _lock:
        _WHICH_CACHE.clear()


@dataclass(frozen=True)
class Spec:
    id: str
    name: str
    command: str
    families: tuple
    package: str = ""    # npm package that provides the command
    native: tuple = ()   # directories (relative to HOME or LOCALAPPDATA) a vendor installer puts the executable in
    update: tuple = ()   # the vendor's own updater arguments, used for native installs only
    check: tuple = ()    # read-only newest-version probe of that updater, if it has one
    aliases: tuple = () # other executable names a provider's configured command may use for this tool


SPECS = (
    Spec("claude", "Claude Code", "claude", ("claude",), "@anthropic-ai/claude-code", ((".local", "bin"),), ("update",)),
    Spec("codex", "Codex CLI", "codex", ("codex",), "@openai/codex"),
    Spec("pi", "Pi", "pi", ("pi",), "@earendil-works/pi-coding-agent"),
    Spec("opencode", "OpenCode", "opencode", ("opencode",), "opencode-ai", ((".opencode", "bin"),), ("upgrade",)),
    Spec("agy", "Antigravity (agy)", "agy", ("antigravity-resilient",), "", (("%LOCALAPPDATA%", "agy", "bin"),), ("update",)),
    Spec("grok", "Grok Build", "grok", ("grok",), "", ((".grok", "bin"),), ("update",), ("update", "--check", "--json"), ("agent",)),
)
BY_ID = {spec.id: spec for spec in SPECS}


@dataclass
class Install:
    spec: Spec
    path: Path | None
    source: str    # npm | native | unknown | missing
    package_dir: Path | None = None
    prefix: Path | None = None
    npm: list | None = None
    reason: str = ""
    env: dict | None = None    # the environment the executable was resolved in, passed on to every command run for it
    configured: str = ""       # the provider's configured command[0], when set


@dataclass
class Target:
    id: str
    name: str
    install: Install
    providers: list


def state_dir() -> Path:
    return Path(os.environ.get("ROUTER_STATE_DIR") or Path.home() / ".agent-compass")


def log(line: str) -> None:
    try:
        path = state_dir() / "cli-updates.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except OSError:
        pass


def same(first, second) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def version_key(text) -> tuple:
    found = VERSION.search(str(text or ""))
    return tuple(int(part) for part in re.findall(r"\d+", found.group(0).split("-")[0])) if found else ()


def clip(text: str, limit: int = 500) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


# ---------- discovery ----------

def paseo_providers(home: Path, environ) -> dict:
    """Name, inheritance, enabled flag, label, command and the PATH of the env of each Paseo provider; no other env."""
    try:
        document = json.loads((Path(environ.get("PASEO_HOME") or home / ".paseo") / "config.json").read_text(encoding="utf-8-sig"))
        raw = document["agents"]["providers"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    rows = {}
    for name, spec in raw.items():
        if isinstance(spec, dict):
            command = spec.get("command")
            raw_env = spec.get("env")
            env = {str(key): value for key, value in raw_env.items()
                   if isinstance(key, str) and isinstance(value, str)} if isinstance(raw_env, dict) else {}
            rows[name] = {"extends": spec.get("extends") if isinstance(spec.get("extends"), str) else None,
                          "enabled": spec.get("enabled") is not False,
                          "label": spec.get("label") if isinstance(spec.get("label"), str) else name,
                          "command": [str(part) for part in command] if isinstance(command, list) else [],
                          "env": env, "path": env.get("PATH", "")}
    return rows


def family_of(name: str, providers: dict) -> str:
    """claude-2 -> claude; a registered ACP launcher (extends acp) is its own family."""
    seen, previous, current = set(), name, name
    while current not in seen:
        seen.add(current)
        if current == "acp":
            return previous
        parent = (providers.get(current) or {}).get("extends")
        if not parent:
            return current
        previous, current = current, parent
    return name


def merged_env(name: str, providers: dict) -> dict:
    """The env Paseo applies for this provider: walk `extends`, and let the child replace the parent."""
    seen, current, env = set(), name, {}
    while current and current not in seen and isinstance(providers.get(current), dict):
        seen.add(current)
        row = providers[current]
        env = {**row.get("env", {}), **env}
        current = row.get("extends")
    return env


def configured_command(name: str, providers: dict) -> tuple[str, str]:
    """The executable a provider's own definition, or the one it extends, names, and the PATH its env sets."""
    seen, current = set(), name
    while current and current not in seen and isinstance(providers.get(current), dict):
        seen.add(current)
        row = providers[current]
        if row["command"]:
            return row["command"][0], row["path"]
        current = row["extends"]
    return "", ""


def prefix_package(prefix: Path, package: str) -> Path | None:
    """The global npm package directory inside `prefix`, only if its package.json names `package`."""
    if not package:
        return None
    parts = package.split("/")
    roots = [prefix.joinpath("node_modules", *parts)]
    if os.name != "nt":
        roots.append(prefix.joinpath("lib", "node_modules", *parts))
    for directory in roots:
        try:
            if json.loads((directory / "package.json").read_text(encoding="utf-8-sig")).get("name") == package:
                return directory
        except (OSError, ValueError, AttributeError):
            continue
    return None


def shim_package(shim: Path, package: str) -> Path | None:
    """The npm package directory a `.cmd` shim launches, only if it is the expected package in the shim's own prefix."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return None
    parts = package.split("/")
    pattern = r"dp0%?[\\/]+node_modules[\\/]+" + r"[\\/]+".join(re.escape(part) for part in parts) + r"[\\/]"
    if not re.search(pattern, text, re.IGNORECASE):
        return None
    return prefix_package(shim.parent, package)


def package_version(directory: Path) -> str:
    try:
        return str(json.loads((directory / "package.json").read_text(encoding="utf-8-sig")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def npm_command(environ) -> list | None:
    """node + npm-cli.js when they sit next to npm (no cmd.exe quoting), else the npm launcher itself."""
    found = _which("npm", environ.get("PATH"))
    if not found:
        return None
    folder = Path(found).parent
    cli = folder / "node_modules" / "npm" / "bin" / "npm-cli.js"
    node = next((folder / name for name in ("node.exe", "node") if (folder / name).is_file()), None)
    return [str(node), str(cli)] if node and cli.is_file() else [found]


def npm_install(spec: Spec, path: Path | None, package_dir: Path, prefix: Path, environ) -> Install:
    npm = npm_command(environ)
    return Install(spec, path, "npm", package_dir, prefix, npm,
                   "" if npm else _tr("找不到 npm，无法更新这个 npm 全局安装"))


def orphaned_npm_on_path(spec: Spec, environ, before_dir: Path | None = None) -> Install | None:
    """An npm package on PATH whose command shim is missing (for instance after an interrupted `npm install -g`)."""
    if not spec.package:
        return None
    for raw in (environ.get("PATH") or "").split(os.pathsep):
        if not raw.strip():
            continue
        entry = Path(raw)
        if before_dir is not None and same(entry, before_dir):
            return None
        prefix = entry.parent if os.name != "nt" and entry.name == "bin" and not (entry / "node_modules").is_dir() else entry
        package_dir = prefix_package(prefix, spec.package)
        if package_dir is not None:
            return npm_install(spec, None, package_dir, prefix, environ)
    return None


def native_directories(spec: Spec, home: Path, environ) -> list:
    roots = {"%LOCALAPPDATA%": Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")}
    return [roots[parts[0]].joinpath(*parts[1:]) if parts[0] in roots else home.joinpath(*parts) for parts in spec.native]


def manual(spec: Spec, path: Path | None, reason: str) -> Install:
    return Install(spec, path, "unknown", reason=reason)


def classify(spec: Spec, path: Path, home: Path, environ) -> Install:
    """What kind of install this exact file is, and so which update method is provable for it."""
    suffix = path.suffix.lower()
    if suffix in (".cmd", ".bat") and spec.package:
        package_dir = shim_package(path, spec.package)
        if package_dir:
            return npm_install(spec, path, package_dir, path.parent, environ)
    elif suffix in (".exe", "") and spec.update and any(same(path.parent, d) for d in native_directories(spec, home, environ)):
        return Install(spec, path, "native")
    return manual(spec, path, _tr(
        "{0} 来自 {1}，不是已确认的 npm 全局安装或官方安装目录，未找到可证明的更新方式；请用安装它的方式手动更新，路由功能不受影响。").format(spec.name, path))


def locate_default(spec: Spec, home: Path, environ) -> Install:
    """The tool a provider without its own command gets: PATH, and Grok Build's installer folder."""
    found = _which(spec.command, environ.get("PATH"))
    orphaned = orphaned_npm_on_path(spec, environ, Path(found).parent if found else None)
    if orphaned is not None:
        return orphaned
    if not found and spec.id == "grok":
        installed = home / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok")
        found = str(installed) if installed.is_file() else None
    return classify(spec, Path(found), home, environ) if found else Install(spec, None, "missing")


def grok_sibling(spec: Spec, path: Path | None) -> Path | None:
    """Grok's `agent` launcher stands for the `grok` executable beside it, when that exists."""
    if path is None or spec.id != "grok" or path.stem.lower() != "agent":
        return path
    sibling = path.with_name("grok" + path.suffix)
    return sibling if sibling.is_file() else path


def locate_configured(spec: Spec, command: str, home: Path, environ, provider_env: dict | None = None) -> Install:
    """The executable a provider's Paseo command names; a configured command is never swapped for another install.

    `provider_env` replaces process variables the way `roll` does (`env.update(provider_env)`). A bare name is
    resolved with that PATH, not by joining it onto the process PATH.
    """
    launch = {**environ, **(provider_env or {})}
    configured = Path(command)
    if configured.stem.lower() not in (spec.command, *spec.aliases):
        install = manual(spec, configured, _tr("为 {0} 配置的命令 {1} 不是 {2} 的可执行文件，无法确定更新方式；路由功能不受影响。").format(
            spec.name, command, spec.command))
    elif configured.name == command:    # a bare name is resolved through PATH, never the current folder
        found = _which(command, launch.get("PATH"))
        resolved = Path(found) if found else None
        orphaned = (orphaned_npm_on_path(spec, launch, resolved.parent if resolved else None)
                    if configured.stem.lower() == spec.command else None)
        if orphaned is not None:
            install = orphaned
        else:
            resolved = grok_sibling(spec, resolved)
            if resolved is None or not resolved.is_file():
                install = manual(spec, resolved or configured, _tr("配置的 {0} 可执行文件不存在：{1}").format(spec.name, command))
            else:
                install = classify(spec, resolved, home, launch)
    else:
        configured = grok_sibling(spec, configured)
        if not configured.is_file():
            prefix = (configured.parent.parent if os.name != "nt" and configured.suffix == "" and configured.parent.name == "bin"
                      else configured.parent if configured.suffix.lower() in (".cmd", ".bat") else None)
            package_dir = (prefix_package(prefix, spec.package)
                           if prefix is not None and configured.stem.lower() == spec.command else None)
            if package_dir is not None:
                install = npm_install(spec, None, package_dir, prefix, launch)
            else:
                install = manual(spec, configured, _tr("配置的 {0} 可执行文件不存在：{1}").format(spec.name, command))
        else:
            install = classify(spec, configured, home, launch)
    install.env = launch
    install.configured = command
    return install


def resolve_command(install: Install, *, fresh: bool = False) -> Path | None:
    """Resolve the CLI executable the same way the launcher resolves it; None when missing or mismatched."""
    if install.source == "missing":
        return None
    if fresh:
        _clear_which_cache()
    environ = install.env if install.env is not None else os.environ
    if install.configured:
        configured = Path(install.configured)
        if configured.name == install.configured:
            found = _which(install.configured, environ.get("PATH"))
            resolved = Path(found) if found else None
        else:
            resolved = configured
        resolved = grok_sibling(install.spec, resolved)
    else:
        found = _which(install.spec.command, environ.get("PATH"))
        if not found and install.spec.id == "grok" and install.path and install.path.is_file():
            found = str(install.path)
        resolved = Path(found) if found else None
    if resolved is None or not resolved.is_file():
        return None
    if install.source == "npm":
        if resolved.suffix.lower() in (".cmd", ".bat"):
            package_dir = shim_package(resolved, install.spec.package)
            if not package_dir or (install.package_dir is not None and not same(package_dir, install.package_dir)):
                return None
        elif install.prefix is not None and not (
            same(resolved.parent, install.prefix) or same(resolved.parent, install.prefix / "bin")
        ):
            return None
    return resolved


def targets(home: Path, environ) -> list[Target]:
    """One target per distinct executable per CLI; accounts that share a binary (claude, claude-2) share a target."""
    providers = paseo_providers(home, environ)
    result = []
    for spec in SPECS:
        names = [name for name in (*spec.families, *(n for n in providers if family_of(n, providers) in spec.families))
                 if providers.get(name, {}).get("enabled", True)]
        groups: dict = {}    # resolved executable -> [install, provider names]
        for name in dict.fromkeys(names):
            command, _env_path = configured_command(name, providers)
            if command:
                install = locate_configured(spec, command, home, environ, merged_env(name, providers))
            else:
                # No configured command still launches with this provider's env. PATH replaces the process PATH.
                launch = {**environ, **merged_env(name, providers)}
                install = locate_default(spec, home, launch)
                install.env = launch
            anchor = install.path or install.prefix
            key = os.path.normcase(str(anchor)) if anchor else ""
            groups.setdefault(key, [install, []])[1].append(name)
        if not names:    # every provider of this CLI is disabled: still show the process-PATH install
            default = locate_default(spec, home, environ)
            if default.source != "missing" or spec.id in DEFAULT_PROVIDERS:
                default.env = dict(environ)
                anchor = default.path or default.prefix
                key = os.path.normcase(str(anchor)) if anchor else ""
                groups[key] = [default, []]
        for key, (install, provider_names) in list(groups.items()):
            if (install.source == "missing" and spec.id not in DEFAULT_PROVIDERS
                    and not any(name in providers for name in provider_names)):
                del groups[key]
        ordered = sorted(groups.items(), key=lambda item: (
            0 if any(name in spec.families for name in item[1][1]) else 1, item[0] != "", item[0]))
        for index, (_, (install, provider_names)) in enumerate(ordered):
            if install.env is None:    # a configured provider already carries its replacing env
                install.env = dict(environ)
            ident = spec.id if index == 0 else f"{spec.id}#{provider_names[0]}"
            result.append(Target(ident, spec.name if index == 0 else f"{spec.name} ({provider_names[0]})",
                                 install, provider_names))
    return result


def find(cli_id: str, home, environ) -> Target | None:
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    return next((target for target in targets(home, environ) if target.id == cli_id), None)


def local_version(install: Install, run_executable: bool = True) -> str:
    """npm: the installed package.json. Others ask the executable itself, which is skipped when run_executable is False."""
    if install.source == "npm":
        return package_version(install.package_dir)
    if run_executable and install.source in ("native", "unknown") and install.path and install.path.suffix.lower() in (".exe", "") \
            and install.path.is_file():
        cache_key = None
        if owned_process.run is _STDLIB_OWNED_RUN:
            try:
                st = install.path.stat()
                cache_key = (os.path.normcase(str(install.path)), st.st_mtime_ns, st.st_size)
            except OSError:
                cache_key = None
            if cache_key is not None:
                with _lock:
                    cached = _VERSION_CACHE.get(cache_key)
                if cached is not None:
                    return cached
        try:
            result = owned_process.run([str(install.path), "--version"], timeout=VERSION_TIMEOUT, env=install.env,
                                       cwd=str(state_dir()) if state_dir().is_dir() else None)
        except owned_process.OwnedProcessError:
            return ""
        found = VERSION.search(result.stdout or result.stderr) if result.returncode == 0 else None
        version = found.group(0) if found else ""
        if cache_key is not None and version:
            with _lock:
                _VERSION_CACHE[cache_key] = version
        return version
    return ""




def snapshot(home=None, environ=None, probe: bool = True) -> list[dict]:
    """Local install versions for the home summary and harness rows; never checks upstream."""
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    return [describe(target, probe) for target in targets(home, environ)]


def describe(target: Target, probe: bool = True) -> dict:
    install, ident = target.install, target.id
    with _lock:
        busy = _busy.get(ident, "")
        latest = _latest.get(ident, "")
    cached = (_load_state().get("items") or {}).get(ident) or {}
    latest = latest or cached.get("latest", "")
    version = local_version(install, probe) if install.source != "missing" else ""
    if install.source == "missing":
        status = "missing"
    elif install.source == "unknown" or install.reason:
        status = "manual"
    elif not resolve_command(install):
        status = "outdated"
    elif cached.get("kind") == "unverified":
        status = "unverified"
    elif latest and version and version_key(latest) > version_key(version):
        status = "outdated"
    elif latest and version:
        status = "current"
    else:
        status = "unchecked"
    can_update = install.source in ("npm", "native") and not install.reason
    return {"id": ident, "name": target.name, "path": str(install.path or ""), "source": install.source,
            "version": version, "latest": latest, "status": status, "providers": target.providers,
            "can_update": can_update, "busy": busy, "note": install.reason}




# ---------- actions ----------

def begin(target: Target) -> str:
    """Claim the CLI for an action; returns '' when claimed, else why not. Only one update runs at a time."""
    with _lock:
        if target.id in _busy:
            return _tr("{0} 正在更新，请稍候").format(target.name)
        if "update" in _busy.values():
            return _tr("另一个 CLI 正在更新，请等它结束后再更新")
        _busy[target.id] = "update"
        return ""


def finish(target: Target, kind: str = "", message: str = "") -> None:
    with _lock:
        _busy.pop(target.id, None)
    if message:
        log(f"{target.id}: {message}")
        def persist(state):
            state.setdefault("items", {})[target.id] = {"latest": _latest.get(target.id, ""), "kind": kind,
                                                       "message": message, "at": time.time()}
        _change_state(persist)


def run_tool(argv: list, timeout: float, install: Install):
    """Run noninteractively with every descendant owned; raises RuntimeError with a short reason on failure."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    options = {"NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_FUND": "false", "NPM_CONFIG_AUDIT": "false"}
    try:
        result = owned_process.run(argv, timeout=timeout, cwd=str(directory), env={**(install.env or os.environ), **options})
    except owned_process.OwnedTimeout as error:
        raise RuntimeError(_tr("{0} 超过 {1} 秒，已停止它启动的全部进程").format(Path(argv[0]).stem, int(timeout))) from error
    except owned_process.OwnedProcessError as error:
        raise RuntimeError(str(error)) from error
    finally:
        _clear_which_cache()
    log(f"$ {' '.join(argv)} -> rc={result.returncode}\n{result.stdout}\n{result.stderr}".rstrip())
    return result


def fail_text(name: str, result) -> str:
    detail = clip(result.stderr or result.stdout, 400)
    return _tr("{0} 更新失败（退出码 {1}）：{2}").format(name, result.returncode, detail or _tr("无输出，详见日志"))


def npm_latest(install: Install) -> str:
    result = run_tool([*install.npm, "view", install.spec.package, "version"], CHECK_TIMEOUT, install)
    found = VERSION.search(result.stdout) if result.returncode == 0 else None
    if not found:
        raise RuntimeError(_tr("查询 {0} 的最新版本失败：{1}").format(
            install.spec.package, clip(result.stderr or result.stdout, 300) or _tr("无输出")))
    return found.group(0)


def native_latest(install: Install) -> tuple[str, bool]:
    result = run_tool([str(install.path), *install.spec.check], CHECK_TIMEOUT, install)
    try:
        value = json.loads(result.stdout)
        latest = str(value["latestVersion"])
        if value.get("error"):
            raise RuntimeError(str(value["error"]))
        return latest, bool(value.get("updateAvailable"))
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(_tr("{0} 的检查结果无法识别：{1}").format(
            install.spec.name, clip(result.stdout or result.stderr, 300) or _tr("无输出"))) from None


def unsupported(target: Target) -> str:
    install = target.install
    if install.source == "missing":
        return _tr("{0} 未安装，没有可更新的内容").format(target.name)
    if install.source not in ("npm", "native") or install.reason:
        return install.reason or _tr("{0} 没有受支持的更新方式").format(target.name)
    return ""






def update(cli_id: str, home=None, environ=None, *, outcome: dict | None = None) -> str:
    """Update one CLI with the method proven for the executable the router resolves; verify the result."""
    cleanup_retired()
    _clear_which_cache()
    target = find(cli_id, home, environ)
    if target is None:
        return _tr("这个启动器没有受支持的更新方式")
    busy = begin(target)
    if busy:
        if outcome is not None:
            outcome.update(status="busy", message=busy)
        return busy
    kind, message, status = "warn", "", "failed"
    try:
        if unsupported(target):
            message = unsupported(target)
            status = "unsupported"
        elif target.install.source == "npm":
            before = package_version(target.install.package_dir)
            had_command = resolve_command(target.install, fresh=True) is not None
            kind, message = update_npm(target)
            after = package_version(target.install.package_dir)
            status = ("updated" if (before != after or not had_command) else "current") if kind == "ok" else "failed"
        else:
            status, message = update_native(target)
            kind = "ok" if status in ("updated", "current") else ("unverified" if status == "unverified" else "warn")
    except (RuntimeError, OSError, ValueError) as error:
        message = _tr("{0} 更新失败：{1}").format(target.name, error)
        status = "failed"
    finally:
        if outcome is not None:
            outcome.update(status=status, message=message)
        finish(target, kind, message)
    return message


def update_npm(target: Target) -> tuple[str, str]:
    install, name = target.install, target.name
    before = package_version(install.package_dir)
    resolved_before = resolve_command(install, fresh=True)
    latest = npm_latest(install)
    with _lock:
        _latest[target.id] = latest
    if resolved_before is not None and before and version_key(before) >= version_key(latest):
        return "ok", _tr("{0} 已是最新（{1}）").format(name, before)
    cli_update_files.prepare_npm(install.package_dir, install.prefix, remember_retired)
    result = run_tool([*install.npm, "install", "-g", f"{install.spec.package}@latest", "--prefix", str(install.prefix),
                       "--no-fund", "--no-audit"], UPDATE_TIMEOUT, install)
    if result.returncode != 0:
        return "warn", fail_text(name, result)
    after = package_version(install.package_dir)
    if not after or version_key(after) < version_key(latest):
        return "warn", _tr("{0} 更新命令已结束，但安装的版本仍是 {1}（目标 {2}），详见日志").format(name, after or "?", latest)
    resolved_after = resolve_command(install, fresh=True)
    if resolved_after is None:
        return "warn", _tr("{0} 更新命令已结束，但命令 {1} 仍无法解析，详见日志").format(
            name, install.configured or install.spec.command)
    install.path = resolved_after
    if resolved_before is None and before == after:
        return "ok", _tr("{0} 已重新安装（{1}）；命令已恢复，未重启任何会话").format(name, after)
    return "ok", _tr("{0} 已更新：{1} → {2}；新版本在下次启动该 CLI 时生效，未重启任何会话").format(name, before or "?", after)


def update_native(target: Target) -> tuple[str, str]:
    """Return a verified outcome, keeping successful command exit separate from version proof."""
    install, name = target.install, target.name
    before = local_version(install)
    resolved_before = resolve_command(install, fresh=True)
    if install.spec.check:
        latest, available = native_latest(install)
        with _lock:
            _latest[target.id] = latest
        if resolved_before is not None and not available:
            return "current", _tr("{0} 已是最新（{1}）").format(name, before or latest)
    result = run_tool([str(install.path), *install.spec.update], UPDATE_TIMEOUT, install)
    if result.returncode != 0:
        return "failed", fail_text(name, result)
    resolved_after = resolve_command(install, fresh=True)
    if resolved_after is None:
        return "failed", _tr("{0} 更新命令已结束，但命令 {1} 仍无法解析，详见日志").format(
            name, install.configured or install.spec.command)
    after = local_version(install)
    if after and before and after != before:
        if not install.spec.check:
            with _lock:
                _latest[target.id] = after
        return "updated", _tr("{0} 已更新：{1} → {2}；新版本在下次启动该 CLI 时生效，未重启任何会话").format(name, before, after)
    if install.spec.check and available:
        return "failed", _tr("{0} 更新失败：更新命令返回成功但版本未变（当前 {1}，目标 {2}）").format(
            name, after or before or "?", latest)
    return "unverified", _tr("{0} 的更新命令返回成功，版本仍为 {1}；无法确认是否已是最新").format(name, after or before or "?")


def open_log() -> str:
    path = state_dir() / "cli-updates.log"
    if not path.exists():
        try:
            import update_status
            return update_status.open_log(state_dir())
        except Exception:
            return _tr("日志不存在：{0}").format(path)
    os.startfile(path)
    return _tr("已打开日志 {0}").format(path.name)


def _load_state() -> dict:
    with _state_lock:
        try:
            value = json.loads((state_dir() / "cli-updates-state.json").read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


def _change_state(change) -> None:
    # The panel and tray watcher live in different processes. Share the router's
    # stable file transaction lock so a completed check cannot undo a checkbox click.
    from router_priority import modules
    modules()
    import override_store as store
    path = state_dir() / "cli-updates-state.json"
    with _state_lock, store.locked(path):
        state = _load_state()
        change(state)
        store.write(path, state)


def get_auto_update() -> bool:
    return _load_state().get("auto_update", False) is True


def set_auto_update(enabled: bool) -> bool:
    _change_state(lambda state: state.update(auto_update=bool(enabled)))
    return bool(enabled)


def remember_retired(path: Path) -> None:
    def persist(state):
        state.setdefault("retired_files", []).append(str(path))
    _change_state(persist)


def cleanup_retired() -> None:
    # Hold the state transaction while deleting, so concurrent panels cannot lose
    # a newly retained image. Windows refuses to unlink a still-mapped last link.
    if not _load_state().get("retired_files"):
        return
    def clean(state):
        state["retired_files"] = [raw for raw in state.get("retired_files", [])
                                  if not cli_update_files.remove_retired(Path(raw))]
    _change_state(clean)


def _record_status(event: dict) -> None:
    try:
        import update_status

        update_status.record(event, directory=state_dir())
    except Exception:
        pass


def update_all(progress=None, *, auto: bool = False, home=None, environ=None) -> dict:
    """Check then update each supported install. All installers are owned; no worker is terminated.

    Returns structured results so notifications never depend on translated text.
    """
    result = {key: [] for key in ("updated", "current", "unverified", "failed")}
    result["messages"] = []
    if not _batch_lock.acquire(blocking=False):
        result["summary"] = _tr("另一个 CLI 正在更新，请等它结束后再更新")
        return result
    claim = ExitStack()
    try:
        from router_priority import modules
        modules()
        import override_store
        try:
            claim.enter_context(override_store.locked(state_dir() / "cli-update-operation", timeout=0))
        except TimeoutError:
            result["summary"] = _tr("另一个 CLI 正在更新，请等它结束后再更新")
            return result
        environ = os.environ if environ is None else environ
        candidates = [target for target in targets(Path(home) if home is not None else Path.home(), environ)
                      if not unsupported(target)]
        if not auto:
            _record_status({
                "kind": "start",
                "title": "CLI",
                "targets": [target.name for target in candidates],
                "message": _tr("开始检查并更新 CLI 工具"),
            })
        for index, target in enumerate(candidates, 1):
            step = _tr("正在检查并更新 ({0}/{1})：{2}").format(index, len(candidates), target.name)
            if not auto:
                _record_status({"kind": "component", "component": target.name, "message": step})
            if progress:
                progress(step)
            outcome = {}
            message = update(target.id, home, environ, outcome=outcome)
            if not auto:
                _record_status({"kind": "result", "component": target.name, "message": message, "status": outcome.get("status", "failed")})
            status = outcome.get("status", "failed")
            if status in result:
                result[status].append(target.id)
            result["messages"].append(message)
        result["summary"] = _tr("CLI：已更新 {0} · 已是最新 {1} · 未确认 {2} · 失败 {3}").format(
            *(len(result[key]) for key in ("updated", "current", "unverified", "failed")))
        if not auto:
            _record_status({"kind": "finish", "component": "", "message": result["summary"]})
        def persist(state):
            state["last_summary"] = result["summary"]
            if auto:
                state["last_auto_update_summary"] = result["summary"]
                state["last_auto_update_at"] = time.time()
        _change_state(persist)
        return result
    finally:
        claim.close()
        _batch_lock.release()


def _timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def maybe_trigger_auto_update(notify_fn=None, *, now: float | None = None, uptime: float | None = None,
                              home=None, environ=None) -> bool:
    """Clean retired files and start nonblocking daily maintenance when enabled."""
    cleanup_retired()
    global _auto_running
    now = time.time() if now is None else now
    uptime = time.monotonic() - _started_at if uptime is None else uptime
    with _state_lock:
        state = _load_state()
        if not state.get("auto_update", False) or uptime < AUTO_UPDATE_STARTUP_SECONDS or _auto_running:
            return False
        with _lock:
            if _busy:
                return False
        due = now - _timestamp(state.get("last_auto_update_at")) >= AUTO_UPDATE_INTERVAL_SECONDS
        if not due:
            return False
        _auto_running = True

    def runner():
        global _auto_running
        try:
            result = update_all(auto=True, home=home, environ=environ)
            if result["updated"] and notify_fn:
                notify_fn(result["summary"])
        except Exception as error:
            log(f"auto-update: {error}")
            # Avoid a failing check every maintenance tick for the rest of the day.
            _change_state(lambda state: state.update(last_auto_update_at=time.time(), last_auto_update_summary=str(error)))
        finally:
            with _state_lock:
                _auto_running = False
    # An in-flight installer must finish its owned process tree before exit.
    worker = threading.Thread(target=runner, name="cli-auto-update", daemon=False)
    try:
        worker.start()
    except Exception:
        with _state_lock:
            _auto_running = False
        raise
    return True
