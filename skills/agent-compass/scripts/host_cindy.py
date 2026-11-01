"""Build the installable Cindy plugin; shell routing needs the plugin's trusted context."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entry


def prepare(out, bundle, prompt_file):
    import roll
    from dispatch_registry import PASEO_LAUNCHER_MODES
    family, _ = roll.provider_definition(bundle['provider'], bundle=bundle)
    if family not in ('claude', 'codex', 'pi'):
        return dict(out, launched=False, dispatch_mode='cindy', error=f'Cindy cannot run routed harness {family!r}',
                    next='Reroute to an exact supported Cindy catalog route; do not substitute.'), 2
    # Fleet bundles normally omit modeId. Reproduce the existing launcher
    # defaults here, then give the plugin a fully explicit permission contract.
    # Pi's CLI has fixed tool permissions (no selectable mode); Cindy expresses
    # that full-tool worker with its explicit bypassPermissions create setting.
    expected = PASEO_LAUNCHER_MODES[family] or 'full-access'
    mode = bundle.get('modeId')
    if mode is None:
        mode = expected
    if mode != expected:
        return dict(out, launched=False, dispatch_mode='cindy',
                    error=f'Cindy cannot reproduce {family!r} permission mode {mode!r}',
                    next='Reroute to a supported permission mode; do not widen an explicitly restricted route.'), 2
    return dict(out, launched=False, dispatch_mode='native', caller_action='cindy_plugin_dispatch',
                route_bundle={**bundle, 'harness': family, 'modeId': mode},
                worker_prompt_file=str(prompt_file),
                next='Return this prepared route to the AgentCompass Cindy plugin. It verifies the official catalog, '
                     'permission and workdir before tasks.create/send. No worker has started.'), 0


def build_plugin(output, mappings=None):
    if not isinstance(mappings or [], list) or any(
        not isinstance(row, dict) or set(row) != {'routerProvider', 'routerAccount', 'providerId', 'agentKind'} or
        row.get('agentKind') not in ('cc', 'codex', 'pi') or
        any(not isinstance(value, str) or not value.strip() for value in row.values())
        for row in mappings or []
    ):
        raise ValueError('Account mappings must name exact routerProvider/routerAccount/providerId/agentKind strings.')
    source = Path(__file__).resolve().parents[1] / 'assets' / 'cindy-plugin'
    if output.exists():
        raise ValueError('Output already exists; choose a new plugin source directory.')
    shutil.copytree(source, output)
    config = {'command': entry.argv('roll'), 'accountMappings': mappings or []}
    (output / 'router.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'status': 'built', 'plugin_source': str(output.resolve()),
            'next_action': 'In Cindy call ghost_forge_pack with this source directory, then install the resulting .cindy '
                           'through Cindy. Approve its Node/sessionContext/tasks capabilities. This does not install anything.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='agentcompass cindy')
    parser.add_argument('action', choices=('build-plugin',))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--account-map', type=Path, help='JSON array of explicit routerProvider/routerAccount/providerId/agentKind bindings')
    args = parser.parse_args(argv)
    try:
        mappings = json.loads(args.account_map.read_text(encoding='utf-8')) if args.account_map else []
        print(json.dumps(build_plugin(args.output, mappings), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
