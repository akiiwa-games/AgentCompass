"""Move plugin clones out of a dev checkout into ~/.agent-compass/plugins/<id> (the product layout).

    python tray/deploy.py migrate-plugins --dry-run [--from CHECKOUT]
    python tray/deploy.py migrate-plugins [--from CHECKOUT] [--plugin ID ...]

A machine that ran AgentCompass from a checkout has the git clones usage-float/, codex-chatgpt-web/ (with local
source patches), gemini-web/ and dot2api/ inside it, sometimes with a junction at ~/.agent-compass/plugins/<id>
pointing back at them. For each clone this stops the plugin's processes, moves the whole working tree (local
changes, untracked and ignored files included) to the product location, rebuilds a Python virtual environment
whose paths moved with it, and starts again what was running.

Safe to repeat: a plugin already at the product location is left alone, and a run interrupted after the copy
finishes removing the old clone the next time. A copy is verified before the old clone is deleted; if anything
fails before then, the old clone and its junction are put back and the plugin is restarted from there. A junction
is only ever unlinked, never followed into.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import plugins

MIGRATABLE = ("usage-float", "chatgpt-web", "gemini-web", "dot2api")
MARKER = ".migrations.json"      # in the plugins directory: {id: {"from": old path}} once a copy is complete
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_NOT_SAME_DEVICE = 17
# Host configs that may name a clone's old path (read only; the report says which to update by hand).
CONFIG_FILES = (".codex/config.toml", ".claude.json", ".claude/settings.json", ".paseo/config.json",
                ".agent-compass/settings.json")


@dataclass
class Step:
    plugin_id: str
    source: Path
    target: Path
    action: str      # move | cleanup | done | absent | conflict
    detail: str = ""
    replace_link: bool = False


def _long(path: Path) -> str:
    text = os.path.abspath(str(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


def is_link(path) -> bool:
    """A symlink or a junction (any reparse point), without following it."""
    info = os.lstat(_long(Path(path)))
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def exists_link(path: Path) -> bool:
    try:
        return is_link(path)
    except OSError:
        return False


def link_target(path: Path) -> Path | None:
    try:
        text = os.readlink(_long(path))
    except OSError:
        return None
    if text.startswith(("\\\\?\\", "\\??\\")):
        text = text[4:]
    return Path(text)


def same_path(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def read_marker(plugins_dir: Path) -> dict:
    try:
        value = json.loads((plugins_dir / MARKER).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marker(plugins_dir: Path, plugin_id: str, source: Path) -> None:
    value = read_marker(plugins_dir)
    value[plugin_id] = {"from": str(source), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    path = plugins_dir / MARKER
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def plan(checkout: Path, plugins_dir: Path, ids=MIGRATABLE) -> list[Step]:
    marker = read_marker(plugins_dir)
    steps = []
    for plugin_id in ids:
        plugin = plugins.BY_ID[plugin_id]
        source = Path(checkout) / (plugin.legacy_dir or plugin_id)
        target = Path(plugins_dir) / plugin_id
        step = Step(plugin_id, source, target, "absent")
        migrated_from = (marker.get(plugin_id) or {}).get("from")
        source_clone = source.is_dir() and not exists_link(source) and (source / ".git").exists()
        old_copy = bool(migrated_from) and same_path(Path(migrated_from), source) and source.exists()             and not exists_link(source)
        if exists_link(target):
            pointed = link_target(target)
            if source_clone and same_path(pointed, source):
                step.action, step.replace_link = "move", True
                step.detail = f"{target} is a junction to the clone; it is replaced by the clone itself"
            elif source_clone:
                step.action, step.detail = "conflict", f"{target} is a link to {pointed}, not to {source}"
            else:
                step.detail = f"{target} is a link to {pointed}; no clone at {source}"
        elif (target / ".git").exists():
            if old_copy:
                step.action, step.detail = "cleanup", f"copy finished earlier; the old clone at {source} is removed"
            elif source_clone:
                step.action, step.detail = "conflict", f"both {source} and {target} are clones; keep one by hand"
            else:
                step.action = "done"
        elif source_clone:
            if target.exists() and any(target.iterdir()):
                step.action, step.detail = "conflict", f"{target} exists and is not a clone"
            else:
                step.action = "move"
        steps.append(step)
    return steps


# ---------- file operations (junction-safe, long paths) ----------

def _children(path: Path) -> list[tuple[Path, bool, bool]]:
    """(child, is_link, is_dir) for each entry; paths stay unprefixed, the OS calls use long paths."""
    rows = []
    with os.scandir(_long(path)) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            link = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
            rows.append((Path(path) / entry.name, link, stat.S_ISDIR(info.st_mode)))
    return rows


def _unlink_link(path: Path) -> None:
    try:
        os.rmdir(_long(path))      # a junction or directory symlink: removes the link, not its target
    except OSError:
        os.unlink(_long(path))


def _writable(path: Path) -> None:
    os.chmod(_long(path), stat.S_IWRITE | stat.S_IREAD)


def remove_tree(path: Path) -> None:
    """Delete a directory tree. Links inside it (and the path itself, if a link) are unlinked, never followed."""
    path = Path(path)
    if exists_link(path):
        _unlink_link(path)
        return
    if not os.path.lexists(_long(path)):
        return
    for child, link, directory in _children(path):
        if link:
            _unlink_link(child)
        elif directory:
            remove_tree(child)
        else:
            try:
                os.unlink(_long(child))
            except PermissionError:
                _writable(child)
                os.unlink(_long(child))
    try:
        os.rmdir(_long(path))
    except PermissionError:
        _writable(path)
        os.rmdir(_long(path))


def make_link(link: Path, target: Path, directory: bool) -> None:
    if os.name == "nt" and directory:
        import _winapi

        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
    else:
        os.symlink(target, link, target_is_directory=directory)


def copy_tree(source: Path, destination: Path) -> None:
    """Copy everything, keeping file times; links are recreated (re-pointed when they pointed inside source)
    once every file is in place, since a junction needs its target to exist."""
    import shutil

    links = []

    def copy(folder: Path, into: Path) -> None:
        os.makedirs(_long(into), exist_ok=True)
        for child, link, directory in _children(folder):
            target = into / child.name
            if link:
                pointed = link_target(child)
                if pointed is None:
                    raise RuntimeError(f"cannot read the link {child}")
                try:
                    pointed = Path(destination) / Path(os.path.abspath(pointed)).relative_to(os.path.abspath(source))
                except ValueError:
                    pass
                links.append((target, pointed, directory or os.path.isdir(_long(child))))
            elif directory:
                copy(child, target)
            else:
                shutil.copy2(_long(child), _long(target))

    copy(Path(source), Path(destination))
    for target, pointed, directory in links:
        make_link(target, pointed, directory)


def inventory(root: Path) -> dict[str, int]:
    """{relative path: size} of every regular file (-1 for a link), not entering links."""
    found, stack = {}, [Path(root)]
    while stack:
        current = stack.pop()
        for child, link, directory in _children(current):
            relative = child.relative_to(root).as_posix()
            if link:
                found[relative] = -1
            elif directory:
                stack.append(child)
            else:
                found[relative] = os.stat(_long(child)).st_size
    return found


def move_tree(source: Path, target: Path, plugins_dir: Path, plugin_id: str) -> str:
    """Rename when the volumes match; otherwise copy to <target>.migrating, verify, rename, record, delete source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
        write_marker(plugins_dir, plugin_id, source)
        return "renamed"
    except OSError as error:
        if getattr(error, "winerror", None) != ERROR_NOT_SAME_DEVICE and error.errno != 18:      # EXDEV
            raise
    staging = target.with_name(target.name + ".migrating")
    remove_tree(staging)
    copy_tree(source, staging)
    if inventory(source) != inventory(staging):
        remove_tree(staging)
        raise RuntimeError(f"the copy of {source} does not match the original; nothing was removed")
    os.rename(staging, target)
    write_marker(plugins_dir, plugin_id, source)
    remove_tree(source)
    return "copied"


# ---------- processes ----------

def process_table() -> list[tuple]:
    """(process, [exe, cwd, *cmdline]) for every process but this one. Enumerating is slow on a busy machine
    (tens of seconds), so a run takes one table and only refreshes it after stopping something."""
    import psutil

    table = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        if proc.info["pid"] == os.getpid():
            continue
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            cwd = None
        table.append((proc, [proc.info.get("exe"), cwd, *(proc.info.get("cmdline") or [])]))
    return table


def processes_under(paths: list[Path], table: list[tuple] | None = None) -> list:
    """Processes whose executable, working directory or a command-line argument lies under one of paths."""
    roots = [os.path.normcase(os.path.abspath(path)).rstrip("\\/") for path in paths]

    def under(text) -> bool:
        if not text:
            return False
        value = os.path.normcase(str(text).strip('"'))
        return any(value == root or value.startswith(root + os.sep) for root in roots)

    return [proc for proc, texts in (process_table() if table is None else table) if any(under(text) for text in texts)]


class Scanner:
    """processes_under over one cached process table; refresh() drops it after something was stopped."""

    def __init__(self):
        self.table = None

    def __call__(self, paths: list[Path]) -> list:
        if self.table is None:
            self.table = process_table()
        return processes_under(paths, self.table)

    def refresh(self) -> None:
        self.table = None


def kill(processes: list) -> None:
    import psutil

    for proc in processes:
        try:
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(processes, timeout=10)


def references(paths: list[Path], home: Path) -> list[str]:
    """Host config files that still name one of the old paths."""
    needles = {variant.lower() for path in paths
               for variant in (str(path), str(path).replace("\\", "/"), str(path).replace("\\", "\\\\"))}
    found = []
    for relative in CONFIG_FILES:
        file = Path(home) / relative
        try:
            text = file.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if any(needle in text for needle in needles):
            found.append(str(file))
    return found


# ---------- the command ----------

def venv_moved(target: Path) -> bool:
    return (target / ".venv" / "pyvenv.cfg").is_file()


def rebuild_venv(target: Path) -> str:
    """A uv virtual environment's launchers embed its old path; recreate it at the new one."""
    import runtime_paths

    remove_tree(target / ".venv")
    subprocess.run([str(runtime_paths.uv_path()), "sync", "--frozen", "--no-dev"], cwd=target, check=True,
                   capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
    return "rebuilt .venv"


def default_stop(plugin_id: str) -> str:
    import services

    return services.plugin_stop(plugin_id)


def default_start(plugin_id: str) -> str:
    import services

    services.refresh_plugin_roots()
    return services.plugin_start(plugin_id)


def migrate(checkout: Path, plugins_dir: Path | None = None, *, dry_run: bool = False, ids=MIGRATABLE,
            home: Path | None = None, stop=default_stop, start=default_start, find=None,
            end=kill, rebuild=rebuild_venv, log=print) -> int:
    plugins_dir = Path(plugins_dir or plugins.PLUGINS_DIR)
    find = find or Scanner()
    home = Path(home or Path.home())
    checkout = Path(checkout).resolve()
    steps = plan(checkout, plugins_dir, ids)
    failures = 0
    log(f"{'[dry run] ' if dry_run else ''}checkout {checkout} -> {plugins_dir}")
    for step in steps:
        line = f"{step.plugin_id}: {step.action}"
        if step.action in ("move", "cleanup"):
            line += f"  {step.source} -> {step.target}"
        if step.detail:
            line += f"  ({step.detail})"
        log(line)
        if step.action == "conflict":
            failures += 1
        if dry_run or step.action not in ("move", "cleanup"):
            if dry_run and step.action == "move":
                running = find([step.source, step.target])
                if running:
                    log(f"  would stop and later restart: {', '.join(str(proc.pid) for proc in running)}")
                if venv_moved(step.source):
                    log("  would rebuild its .venv (uv sync) at the new path")
            continue
        if step.action == "cleanup":
            remove_tree(step.source)
            log(f"  removed the old clone {step.source}")
            continue
        running = find([step.source, step.target])
        if running:
            log(f"  stop: {stop(step.plugin_id)}")
            getattr(find, "refresh", lambda: None)()
            leftovers = find([step.source, step.target])
            if leftovers:
                end(leftovers)
                log(f"  ended {len(leftovers)} leftover process(es)")
        try:
            if step.replace_link:
                if not exists_link(step.target):
                    raise RuntimeError(f"{step.target} changed under us")
                remove_tree(step.target)      # unlinks the junction only
            how = move_tree(step.source, step.target, plugins_dir, step.plugin_id)
            log(f"  moved ({how})")
        except Exception as error:
            failures += 1
            log(f"  move failed: {error}")
            if step.replace_link and not step.target.exists() and step.source.is_dir():
                make_link(step.target, step.source, True)
                log(f"  restored the junction {step.target} -> {step.source}")
            if running:
                log(f"  start (old location): {start(step.plugin_id)}")
            continue
        if venv_moved(step.target):
            try:
                log(f"  {rebuild(step.target)}")
            except Exception as error:
                failures += 1
                log(f"  .venv rebuild failed: {error}; run uv sync --frozen --no-dev in {step.target}")
        if running:
            log(f"  start: {start(step.plugin_id)}")
    moved = [step.source for step in steps if step.action in ("move", "cleanup", "done")]
    for file in references(moved, home):
        log(f"note: {file} still names an old checkout path; update it by hand")
    return 1 if failures else 0
