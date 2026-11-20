"""Host-neutral workspace directory validation and per-host directory placement.

`--workspace <dir>` is a host-neutral placement claim: an existing directory that
this worker writes in and claims exclusively. Core code validates and canonicalizes
the directory once; each launch surface maps that canonical directory to its own
native placement (`cwd`, `--cwd`, or native orchestrator instructions) through
`place()` and reports structured refusal when it cannot place the directory.
"""
import os
from pathlib import Path

import launch_values

REMOTE_SURFACES = ('dot', 'chatgpt-web', 'grok-bot')


def parent_cwd():
    """The parent's directory: a local worker without `--workspace` starts here, whatever surface launches it. Paseo
    gives the agent PASEO_AGENT_ID names its directory in PASEO_AGENT_CWD; without that id the variable is no parent's
    of this roll (a Herdr, native or cli launch run outside the Paseo session inherits it), and the worker starts where
    the command runs."""
    if (os.environ.get('PASEO_AGENT_ID') or '').strip() and os.environ.get('PASEO_AGENT_CWD'):
        return os.environ['PASEO_AGENT_CWD']
    return os.getcwd()


def worker_directory(workspace, resume=None):
    """The one answer to where a local worker runs: the canonical `--workspace` when given (a resumed worker too: the
    host resumes a worker only in its own directory); else, for a worker roll resumes (`resume`, its agent id), its own
    directory from its Paseo record, None when that cannot be read; else `parent_cwd()`. roll puts it in the task
    (`task['cwd']`), so the plan, its re-plan at the launch, `record` and a named roll match an account's `cwd_regex`
    against it, and every launch surface places the worker there (`place`)."""
    if ws := canonical_dir(workspace):
        return ws
    if resume:
        import dispatch_registry    # imports this module
        own = (dispatch_registry.paseo_agent_record(resume) or {}).get('cwd')
        return own if isinstance(own, str) and own.strip() else None
    return parent_cwd()


class InvalidWorkspace(ValueError):
    """`--workspace` was given a value that is not an existing directory."""

    def __init__(self, raw, message):
        super().__init__(message)
        self.raw = raw


def canonical_dir(raw_dir):
    """Canonical path string (resolved, normpath, normcased) for directory comparisons, or None."""
    if raw_dir is None:
        return None
    text = str(raw_dir).strip()
    if not text:
        return None
    expanded = Path(text).expanduser()
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(str(expanded)))
    return os.path.normcase(os.path.normpath(str(resolved)))


def validate_workspace(raw_dir):
    """Validate `--workspace` once before classification, allocation or launch; return canonical_dir."""
    if raw_dir is None:
        return None
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise InvalidWorkspace(raw_dir, '--workspace requires an existing directory path.')
    raw_text = raw_dir.strip()
    candidate = Path(raw_text).expanduser()
    try:
        exists = candidate.exists()
        is_dir = candidate.is_dir()
    except OSError as exc:
        raise InvalidWorkspace(raw_dir, f'--workspace path could not be accessed ({raw_text}): {exc}') from exc
    if not is_dir:
        if exists:
            raise InvalidWorkspace(raw_dir, f'--workspace must be an existing directory, not a file: {raw_text}')
        hint = (' (--workspace takes a directory path, never a host workspace ID such as wks_...)'
                if raw_text.startswith('wks_') else '')
        raise InvalidWorkspace(raw_dir, f'--workspace directory does not exist: {raw_text}{hint}')
    return canonical_dir(candidate)


def native_next(host, bundle, address, workspace=None):
    """What the calling session does to start the routed worker with its own host's orchestrator."""
    provider, model, effort = bundle.get('provider'), bundle.get('model'), bundle.get('effort')
    if host == 'orca':      # every value quoted for the shell the parent runs it in (launch_values.shell_word)
        flags = (f'--agent {launch_values.shell_word(provider)} --worktree current'
                 + (f' --model {launch_values.shell_word(model)}' if model else '')
                 + (f' --effort {launch_values.shell_word(effort)}' if effort else ''))
        where = (f' Run the command from directory `{workspace}` so `--worktree current` places the worker there.'
                 if workspace else '')
        return ('Start the worker with Orca orchestration, once, using the executable you ran `skills get` with: '
                f'`orca orchestration worker-start --spec "<contents of worker_prompt_file>" {flags} --json` (run `run-create` first if no '
                f'Run is bound).{where} Use no other sub-agent tool. The worker reports to {address} through its injected preamble; collect it with '
                '`orca orchestration check --wait --types worker_done,escalation,question --json`. If Orca cannot run this '
                'provider/model/effort, reroute instead of substituting.')
    if host == 't3':
        where = f', working directory `{workspace}`' if workspace else ''
        return ('Start the worker with the T3 Code MCP tool `delegate_task` (`orchestrator_capabilities` first, once per thread): '
                f'task = the full text of worker_prompt_file, target.driverKind/model from route.bundle ({provider} / {model}){where}, mode `async`. '
                'T3 delivers the result to this parent thread; do not poll `task_status` or use another sub-agent tool. If T3 offers no '
                'provider/model matching route.bundle, reroute instead of substituting.')
    where = f' in working directory `{workspace}`' if workspace else ''
    return (f'Launch the native host worker{where} with exactly route.bundle '
            'and the contents of worker_prompt_file as --spec (not the filename). First verify that '
            'the host can reproduce the routed provider/account/model/effort; otherwise reroute. Preserve '
            'the live coordinator inbox, parent session ID and injected worker preamble; do not switch hosts.')


def place(surface, workspace, *, default_cwd=None, bundle=None, address=None, paseo_probe=None):
    """Map canonical `workspace` (or None) to host-native placement for `surface`: a remote runner
    (REMOTE_SURFACES), `paseo`, `herdr`, `cli` or a native host (worker_context.NATIVE_HOSTS), the surfaces roll builds."""
    if surface in REMOTE_SURFACES:
        if workspace:
            return {
                'ok': False,
                'surface': surface,
                'workspace': workspace,
                'error': 'host_cannot_place_directory',
                'caller_action': 'check_error',
                'next': (f'Nothing was launched: {surface} runs remotely and cannot place a worker in local directory '
                         f'{workspace}. Omit --workspace for remote work, or route to a local provider.'),
            }
        return {'ok': True, 'surface': surface, 'workspace': None, 'cwd': None}
    if surface == 'paseo':
        if workspace and isinstance(paseo_probe, dict) and paseo_probe.get('placeable') is False:
            err = paseo_probe.get('error') or 'paseo_workspace_not_found_for_directory'
            enclosing = [str(d) for d in (paseo_probe.get('enclosing_directories') or ()) if d]
            if err == 'paseo_workspaces_unreadable':
                nxt = (f'Nothing was launched: Paseo workspaces could not be read to verify placement for {workspace}, '
                       'and Paseo cannot place a sub-agent in an unverified directory. Check the local Paseo daemon, '
                       'then roll again.')
            else:
                enc_note = (f' Paseo only has workspace(s) rooted at {", ".join(enclosing)} '
                            f'(never auto-picked, because Paseo would place the worker there instead of {workspace}).'
                            if enclosing else '')
                nxt = (f'Nothing was launched: Paseo has no workspace rooted at {workspace}, and Paseo createAgent '
                       f'with callerAgentId cannot place a sub-agent in an arbitrary directory without a matching '
                       f'workspace.{enc_note} Register {workspace} as a Paseo workspace first, or omit --workspace '
                       'when the worker creates its own worktree.')
            return {
                'ok': False,
                'surface': 'paseo',
                'workspace': workspace,
                'error': err,
                'caller_action': 'check_error',
                'next': nxt,
            }
        landing = workspace or default_cwd
        return {
            'ok': True,
            'surface': 'paseo',
            'workspace': workspace,
            'cwd': landing,
            'launch': {'cwd': workspace} if workspace else {},
        }
    if surface in ('herdr', 'cli'):
        landing = workspace or default_cwd or str(Path.cwd())
        return {'ok': True, 'surface': surface, 'workspace': workspace, 'cwd': landing}
    # A native host (worker_context.NATIVE_HOSTS): roll builds no other surface.
    landing = workspace or default_cwd or str(Path.cwd())
    return {
        'ok': True,
        'surface': surface,
        'workspace': workspace,
        'cwd': landing,
        'next': native_next(surface, bundle or {}, address, workspace=workspace),
    }
