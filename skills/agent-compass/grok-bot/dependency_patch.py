"""Small dependency-only scope correction for fixed upstream PR158 artifacts.

Contract: a native request uses a route obtained for the same active account/team.
Cached descriptors do not prove team ownership, so bootstrap through upstream
credentials/EnsureSandBox. Reject scope changes before delivery; missing/revoked
credentials stay setup failures. No token refresh, auth copying, or write replay.
"""
import hashlib
import json
from pathlib import Path

CONNECT = '''        function boundCredentials(state) {
            const current = (0, _app_session_js__rspack_import_2.gF)({
                ...state.appSessionOptions, expectedAccountKey: state.accountKey
            });
            if (!current || current.accountKey !== state.accountKey || current.teamId !== state.teamId) {
                throw new GatewayError("Grok Bot account/team changed; route discarded before delivery.");
            }
            return current;
        }
        async function connectGateway({ signal, appSessionOptions } = {}) {
            assertGatewayActive(signal);
            const override = gatewayOverride();
            if (override) return override;
            const token = accessTokenFromEnv();
            if (token) {
                const session = await ensureSandbox(token, { signal });
                reconnectStates.set(session, { accessToken: token, generation: 0, pending: null });
                return session;
            }
            // A cached descriptor has no team binding. Obtain a route for this native scope.
            const credentials = (0, _app_session_js__rspack_import_2.gF)(appSessionOptions);
            if (!credentials?.accessToken) {
                throw new GatewayError("No active Grok Bot credentials; complete official app authentication.");
            }
            const state = { accountKey: credentials.accountKey, teamId: credentials.teamId,
                appSessionOptions, generation: 0, pending: null };
            const session = await ensureSandbox(credentials.accessToken, { signal, teamId: credentials.teamId });
            boundCredentials(state);
            assertGatewayActive(signal);
            reconnectStates.set(session, state);
            return session;
        }
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Pinned upstream patch anchor differs')
    return text.replace(old, new, 1)


def patch_bundle(name, content):
    spec = json.loads((Path(__file__).parent / 'upstream.json').read_text(encoding='utf-8'))
    if hashlib.sha256(content).hexdigest() != spec['artifact_sha256'][name]:
        raise ValueError('Unreviewed upstream bundle')
    text = content.decode('utf-8')
    start = text.index('        async function connectGateway(')
    end = text.index('        async function refreshGateway(', start)
    text = text[:start] + CONNECT + text[end:]
    text = replace_once(text, '''const credentials = state.accountKey ? (0, _app_session_js__rspack_import_2.gF)({
                        ...state.appSessionOptions,
                        expectedAccountKey: state.accountKey
                    }) : {''', '''const credentials = state.accountKey ? boundCredentials(state) : {''')
    text = replace_once(text, '''if (state.accountKey && (0, _app_session_js__rspack_import_2.B3)(state.appSessionOptions) !== state.accountKey) {
                        throw new GatewayError("Grok Bot active account changed during reconnect; refreshed route discarded.");
                    }''', '''if (state.accountKey) boundCredentials(state);''')
    text = replace_once(text, 'const reconnectState = reconnectStates.get(session);',
                        'const reconnectState = reconnectStates.get(session);\n            if (reconnectState?.accountKey) boundCredentials(reconnectState);')
    # A legacy top-level team cannot be silently interpreted as a personal account.
    anchor = 'if (typeof accounts?.active !== "string" || !Object.hasOwn(accounts.accounts ?? {}, accounts.active)) return null;'
    text = replace_once(text, anchor, anchor + '''
            if (!/^[0-9a-f]{64}$/.test(accounts.active)) {
                throw new GrokBotGatewaySessionError("UNSUPPORTED_ACCOUNT_SCOPE", "Unsupported native account scope.");
            }
            if (accounts.accounts[accounts.active]["cursor-selected-team-id"] == null && stored["cursor-selected-team-id"] != null) {
                throw new GrokBotGatewaySessionError("LEGACY_TEAM_SCOPE", "Resolve legacy selected-team storage in the official app.");
            }''')
    return text.encode('utf-8')
