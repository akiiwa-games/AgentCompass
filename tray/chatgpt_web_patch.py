"""Local patches on the codex-chatgpt-web source tree that the ChatGPT Web bridge runs from.

The tree is a git clone of the upstream project, so each patch is a handful of anchored line edits that are
applied after a pull and taken back with `git checkout` before the next one. Together they

  - give the launcher no tray icon of its own when the AgentCompass tray starts it with
    CODEX_WEB_GPT_EXTERNAL_TRAY=1: it hides on close rather than quitting, and quits on `--quit`;
  - recognise the Chinese "停止" (Stop) button of the ChatGPT composer, which upstream only knows in English;
  - report the canonical https://chatgpt.com/c/<id> address of the open chat in the diagnostic state, so a request
    whose answer was lost can be found again;
  - wait, after the helper navigates to the chat page, until the page has a real viewport: while a hidden launcher
    window is loading the page its viewport is 0x0 (the launcher applies the emulated size at did-finish-load), and a
    slow load made the first click fail with "Element is outside of the viewport" (2026-10-10);
  - log the model and effort controls actually seen when a model selection passes, as the Pro channel repair of
    2026-10-08 did (diagnostics/model-selection-evidence.jsonl in the bridge home).

    python chatgpt_web_patch.py status|apply|revert|fits [--root PATH]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugins

SOURCE = plugins.plugin_root("chatgpt-web")
MARK = "agent-compass:"
LEGACY_MARKS = ("route-manager:",)      # trees patched before the AgentCompass rename; renamed on the next apply
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

MAIN = "launcher/electron/main.cjs"
SESSION = "src/chatgpt-session.ts"
WORKER = "src/adapters/chatgpt-web/browser-worker.ts"
SELECTION = "src/adapters/chatgpt-web/model-selection.ts"

STOP_BUTTON = "form[data-chatgpt-composer] button[type=\"button\"][aria-label=\"Stop\"]"
STOP_ICON_BUTTON = "path[d=\"${CHATGPT_STOP_ICON_PATH}\"])`"
CONVERSATION_URL = "/^https:\\/\\/chatgpt\\.com\\/c\\/[A-Za-z0-9-]+\\/?$/"

# (anchor, replacement) per file. An anchor is part of one line, so the file's own line endings do not matter;
# {eol} in a replacement is the file's. Braces that are code are doubled.
EDITS: dict[str, list[tuple[str, str]]] = {
    MAIN: [
        ("let tray = null;",
         "let tray = null;{eol}const EXTERNAL_TRAY = process.env.CODEX_WEB_GPT_EXTERNAL_TRAY === \"1\"; // " + MARK + "external-tray"),
        ("function createTray(logger, language) {",
         "function createTray(logger, language) {{{eol}  if (EXTERNAL_TRAY) return true;"),
        ("if (stateStore.read().keepRunningOnClose && tray) window.hide();",
         "if (stateStore.read().keepRunningOnClose && (tray || EXTERNAL_TRAY)) window.hide();"),
        ("app.on(\"second-instance\", () => showMainWindow());",
         "if (EXTERNAL_TRAY && process.argv.includes(\"--quit\")) {{ app.quit(); return; }}{eol}"
         "  app.on(\"second-instance\", (_event, argv) => {{{eol}"
         "    if (EXTERNAL_TRAY && argv.includes(\"--quit\")) void requestQuit();{eol}"
         "    else showMainWindow();{eol}"
         "  }});"),
    ],
    SESSION: [
        (f"{STOP_BUTTON}';",
         f"{STOP_BUTTON}, form[data-chatgpt-composer] button[type=\"button\"][aria-label=\"停止\"]'; // " + MARK + "chinese-stop"),
    ],
    WORKER: [
        ("pathSegments: currentUrl.pathname.split(\"/\").filter(Boolean).length,",
         "conversationUrl: currentUrl.origin === \"https://chatgpt.com\" && " + CONVERSATION_URL
         + ".test(currentUrl.pathname) ? currentUrl.origin + currentUrl.pathname : null, // " + MARK + "conversation-url{eol}"
         "              pathSegments: currentUrl.pathname.split(\"/\").filter(Boolean).length,"),
        ("return CHATGPT_DIAGNOSTIC_SAFE_STRING_KEYS.has(key) && candidate.length <= 200",
         "return (key === \"conversationUrl\" ? " + CONVERSATION_URL
         + ".test(candidate) : CHATGPT_DIAGNOSTIC_SAFE_STRING_KEYS.has(key)) && candidate.length <= 200 // " + MARK + "conversation-url"),
        ("async function waitForOperationalChatGptViewport(page: Page, signal?: AbortSignal): Promise<void> {",
         "async function waitForOperationalChatGptViewport(page: Page, signal?: AbortSignal, timeoutMs = 10_000): Promise<void> {{ // "
         + MARK + "viewport-after-navigation"),
        ("{ polling: 50, timeout: 10_000 },",
         "{{ polling: 50, timeout: timeoutMs }},"),
        ("await captureDiagnostic?.(useSavedChats ? \"saved-chat-navigation-complete\" : \"temporary-chat-navigation-complete\");",
         "if (typeof page.waitForFunction === \"function\") await waitForOperationalChatGptViewport(page, abortSignal, 45_000); // " + MARK + "viewport-after-navigation{eol}"
         "      await captureDiagnostic?.(useSavedChats ? \"saved-chat-navigation-complete\" : \"temporary-chat-navigation-complete\");"),
    ],
    SELECTION: [
        ("import { ChatGptWebAdapterError } from \"./adapter-error\";",
         "import {{ ChatGptWebAdapterError }} from \"./adapter-error\";{eol}"
         "import {{ appendFileSync, mkdirSync }} from \"node:fs\"; // " + MARK + "model-evidence{eol}"
         "import {{ homedir }} from \"node:os\";{eol}"
         "import {{ join }} from \"node:path\";"),
        ("if (checked && state && state.value === state.min + effortIndex && chatGptModelFamilyMatches(descriptions, family, effort)) return;",
         "if (checked && state && state.value === state.min + effortIndex && chatGptModelFamilyMatches(descriptions, family, effort)) {{{eol}"
         "      try {{ // " + MARK + "model-evidence{eol}"
         "        const directory = join(process.env.CODEX_CHATGPT_WEB_HOME?.trim() || join(homedir(), \".codex-chatgpt-web\"), \"diagnostics\");{eol}"
         "        mkdirSync(directory, {{ recursive: true }});{eol}"
         "        appendFileSync(join(directory, \"model-selection-evidence.jsonl\"), JSON.stringify({{{eol}"
         "          observed_at_utc: new Date().toISOString(), helper_pid: process.pid, requested_family: family,{eol}"
         "          requested_effort: effort, ui_effort_index: effortIndex, range: state,{eol}"
         "          checked_radio_text: await option.innerText(), aria_descriptions: descriptions,{eol}"
         "          original_all_model_checks_passed: true,{eol}"
         "        }}) + \"\\n\");{eol}"
         "      }} catch {{}}{eol}"
         "      return;{eol}"
         "    }}"),
    ],
}


ALTERNATIVE_EDITS: dict[str, list[tuple[str, str]]] = {
    'await captureDiagnostic?.(useSavedChats ? "saved-chat-navigation-complete" : "temporary-chat-navigation-complete");': [
        ('await captureDiagnostic?.("new-chat-navigation-complete");',
         'if (typeof page.waitForFunction === "function") await waitForOperationalChatGptViewport(page, abortSignal, 45_000); // '
         + MARK + 'viewport-after-navigation{eol}      await captureDiagnostic?.("new-chat-navigation-complete");'),
    ],
    "app.on(\"second-instance\", () => showMainWindow());": [
        ("app.on(\"second-instance\", () => showMainWindow());",
         "app.on(\"second-instance\", (_event, argv) => {{{eol}"
         "    if (EXTERNAL_TRAY && argv.includes(\"--quit\")) void requestQuit();{eol}"
         "    else showMainWindow();{eol}"
         "  }});"),
    ],
    f"{STOP_BUTTON}';": [
        (f"{STOP_ICON_BUTTON};",
         f"{STOP_ICON_BUTTON.replace('{', '{{').replace('}', '}}')} + ', {STOP_BUTTON}, form[data-chatgpt-composer] button[type=\"button\"][aria-label=\"停止\"]'; // "
         + MARK + "chinese-stop"),
    ],
}


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def marked(source: str) -> bool:
    return any(mark in source for mark in (MARK, *LEGACY_MARKS))


def current_marks(source: str) -> str:
    for mark in LEGACY_MARKS:
        source = source.replace(mark, MARK)
    return source


def patch_source(source: str, edits: list[tuple[str, str]]) -> str:
    """The file with the edits applied; only the markers renamed when they already are; ValueError when an anchor
    has moved."""
    if marked(source):
        return current_marks(source)
    eol = "\r\n" if "\r\n" in source else "\n"
    for anchor, replacement in edits:
        if source.count(anchor) != 1:
            matched = next(
                ((alt_anchor, alt_rep) for alt_anchor, alt_rep in ALTERNATIVE_EDITS.get(anchor, ())
                 if source.count(alt_anchor) == 1),
                None,
            )
            if matched is None:
                raise ValueError(f"expected one `{anchor[:70]}`, found {source.count(anchor)} (upstream changed it?)")
            anchor, replacement = matched
        source = source.replace(anchor, replacement.format(eol=eol))
    return source


def status(root: Path = SOURCE) -> str:
    """patched, original, partial (some files only), or missing (no source tree)."""
    states = []
    for name in EDITS:
        path = root / name
        if not path.is_file():
            return "missing"
        states.append(marked(read(path)))
    return "patched" if all(states) else "original" if not any(states) else "partial"


def fits(root: Path = SOURCE) -> str | None:
    """None when every patch applies to this tree, else why it does not (an upstream change moved an anchor)."""
    try:
        for name, edits in EDITS.items():
            patch_source(read(root / name), edits)
    except (OSError, ValueError) as error:
        return f"{name}: {error}"
    return None


def apply(root: Path = SOURCE) -> str:
    """Patch the tree. Nothing is written unless every file patches."""
    current = status(root)
    if current == "patched":
        for name in EDITS:
            text = read(root / name)
            if current_marks(text) != text:
                write(root / name, current_marks(text))
    if current in ("patched", "missing"):
        return current
    if current == "partial":
        raise RuntimeError("some files are patched and some are not; run revert first")
    patched = {}
    for name, edits in EDITS.items():
        try:
            patched[name] = patch_source(read(root / name), edits)
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from error
    for name, text in patched.items():
        write(root / name, text)
    return "patched"


def revert(root: Path = SOURCE) -> str:
    """Remove our exact edits without discarding unrelated changes in the same file."""
    restored = {}
    for name, edits in EDITS.items():
        path = root / name
        if not path.is_file() or not marked(read(path)):
            continue
        source = current_marks(read(path))
        eol = "\r\n" if "\r\n" in source else "\n"
        for original, replacement in reversed(edits):
            candidates = [
                (orig, rep.format(eol=eol))
                for orig, rep in [(original, replacement), *ALTERNATIVE_EDITS.get(original, ())]
                if source.count(rep.format(eol=eol)) == 1
            ]
            if not candidates:
                raise RuntimeError(f"cannot separate the local adapter in {name}; source retained")
            original, applied = max(candidates, key=lambda pair: len(pair[1]))
            source = source.replace(applied, original, 1)
        restored[path] = source
    for path, source in restored.items():
        write(path, source)
    return status(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "apply", "revert", "fits"))
    parser.add_argument("--root", type=Path, default=SOURCE)
    args = parser.parse_args()
    try:
        print({"status": status, "apply": apply, "revert": revert, "fits": lambda root: fits(root) or "fits"}[args.action](args.root))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
