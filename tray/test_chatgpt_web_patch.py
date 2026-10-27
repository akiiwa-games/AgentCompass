import subprocess
import tempfile
import unittest
from pathlib import Path

import chatgpt_web_patch as patch

MAIN = "\n".join([
    "let tray = null;",
    "function createTray(logger, language) {",
    "  try {",
    "    tray = new Tray(trayImage());",
    "  } catch (error) { return false; }",
    "}",
    "window.on(\"close\", () => {",
    "  if (stateStore.read().keepRunningOnClose && tray) window.hide();",
    "});",
    "app.on(\"second-instance\", () => showMainWindow());",
    "",
])
SESSION = (
    "export const CHATGPT_STOP_BUTTON_SELECTOR = '[data-testid=\"stop-button\"], "
    "form[data-chatgpt-composer] button[type=\"button\"][aria-label=\"Stop\"]';\n"
)
WORKER = "\n".join([
    "          return {",
    "            location: {",
    "              origin: currentUrl.origin,",
    "              pathSegments: currentUrl.pathname.split(\"/\").filter(Boolean).length,",
    "            },",
    "          };",
    "    if (typeof candidate === \"string\") {",
    "      return CHATGPT_DIAGNOSTIC_SAFE_STRING_KEYS.has(key) && candidate.length <= 200",
    "        ? [[key, candidate]]",
    "        : [];",
    "    }",
    "async function waitForOperationalChatGptViewport(page: Page, signal?: AbortSignal): Promise<void> {",
    "  await page.waitForFunction(",
    "    fits,",
    "    { polling: 50, timeout: 10_000 },",
    "  );",
    "}",
    "      await captureDiagnostic?.(useSavedChats ? \"saved-chat-navigation-complete\" : \"temporary-chat-navigation-complete\");",
    "",
])
SELECTION = "\n".join([
    "import { ChatGptWebAdapterError } from \"./adapter-error\";",
    "",
    "export async function assertChatGptModelFamily() {",
    "  do {",
    "    const option = familyOption(menu, family);",
    "    if (checked && state && state.value === state.min + effortIndex && chatGptModelFamilyMatches(descriptions, family, effort)) return;",
    "  } while (true);",
    "}",
    "",
])
FILES = {patch.MAIN: MAIN, patch.SESSION: SESSION, patch.WORKER: WORKER, patch.SELECTION: SELECTION}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


class SourcePatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "test")
        self.originals = {}
        for name, text in FILES.items():
            self.put(name, text)
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "upstream")

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        self.originals[name] = path.read_bytes()

    def text(self, name):
        return (self.root / name).read_bytes().decode("utf-8")

    def test_reverting_adapter_preserves_other_features_in_the_same_source_file(self):
        patch.apply(self.root)
        path = self.root / patch.MAIN
        extra = b'\nfunction myExistingFeature() { return 42; }\n'
        path.write_bytes(path.read_bytes() + extra)
        patch.revert(self.root)
        self.assertEqual(path.read_bytes(), self.originals[patch.MAIN] + extra)
        patch.apply(self.root)
        self.assertIn("myExistingFeature", self.text(patch.MAIN))

    def test_every_file_is_patched_once_and_a_second_apply_changes_nothing(self):
        self.assertEqual(patch.status(self.root), "original")
        self.assertEqual(patch.apply(self.root), "patched")
        after = {name: self.text(name) for name in FILES}
        self.assertEqual(patch.apply(self.root), "patched")
        self.assertEqual({name: self.text(name) for name in FILES}, after)
        self.assertEqual(patch.status(self.root), "patched")

    def test_the_launcher_patch_removes_its_tray_icon_and_quits_on_request(self):
        patch.apply(self.root)
        main = self.text(patch.MAIN)
        self.assertIn("CODEX_WEB_GPT_EXTERNAL_TRAY", main)
        self.assertIn("if (EXTERNAL_TRAY) return true;", main)
        self.assertIn("(tray || EXTERNAL_TRAY)) window.hide();", main)
        self.assertIn("argv.includes(\"--quit\")) void requestQuit();", main)

    def test_the_chinese_stop_button_is_recognised_next_to_the_english_one(self):
        patch.apply(self.root)
        selector = self.text(patch.SESSION)
        self.assertIn("[aria-label=\"Stop\"]", selector)
        self.assertIn("[aria-label=\"停止\"]'", selector)

    def test_only_the_canonical_chat_address_is_reported_and_kept(self):
        patch.apply(self.root)
        worker = self.text(patch.WORKER)
        self.assertEqual(worker.count("chatgpt\\.com\\/c\\/[A-Za-z0-9-]+"), 2)
        self.assertIn("conversationUrl: currentUrl.origin === \"https://chatgpt.com\"", worker)
        self.assertIn("key === \"conversationUrl\" ?", worker)

    def test_the_viewport_is_awaited_after_navigation_before_the_checkpoint_and_anything_after_it(self):
        old = 'await captureDiagnostic?.(useSavedChats ? "saved-chat-navigation-complete" : "temporary-chat-navigation-complete");'
        for checkpoint in (old, 'await captureDiagnostic?.("new-chat-navigation-complete");'):
            with self.subTest(checkpoint=checkpoint):
                original = WORKER.replace(old, checkpoint)
                self.put(patch.WORKER, original)
                patch.apply(self.root)
                worker = self.text(patch.WORKER)
                self.assertIn("signal?: AbortSignal, timeoutMs = 10_000): Promise<void> {", worker)
                self.assertIn("{ polling: 50, timeout: timeoutMs }", worker)
                self.assertNotIn("timeout: 10_000 }", worker)
                wait = worker.index("if (typeof page.waitForFunction === \"function\") await waitForOperationalChatGptViewport(page, abortSignal, 45_000);")
                self.assertLess(wait, worker.index(checkpoint))
                patch.revert(self.root)
                self.assertEqual(self.text(patch.WORKER), original)

    def test_the_model_evidence_log_is_written_only_after_the_checks_pass(self):
        patch.apply(self.root)
        selection = self.text(patch.SELECTION)
        self.assertIn("model-selection-evidence.jsonl", selection)
        self.assertIn("original_all_model_checks_passed: true", selection)
        self.assertLess(selection.index("chatGptModelFamilyMatches"), selection.index("appendFileSync(join"))

    def test_patched_code_keeps_its_braces_balanced(self):
        patch.apply(self.root)
        for name in FILES:
            patched = self.text(name)
            original = self.originals[name].decode("utf-8")
            self.assertEqual(patched.count("{") - patched.count("}"), original.count("{") - original.count("}"), name)
            self.assertNotIn("{eol}", patched)

    def test_revert_gives_back_the_upstream_files_byte_for_byte(self):
        patch.apply(self.root)
        self.assertEqual(patch.revert(self.root), "original")
        for name, data in self.originals.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual(patch.revert(self.root), "original")

    def test_line_endings_of_a_crlf_file_are_kept(self):
        self.put(patch.MAIN, MAIN.replace("\n", "\r\n"))
        patch.apply(self.root)
        main = (self.root / patch.MAIN).read_bytes()
        self.assertEqual(main.count(b"\n"), main.count(b"\r\n"))

    def test_a_moved_anchor_is_reported_and_nothing_is_written(self):
        self.put(patch.SESSION, SESSION.replace("aria-label=\"Stop\"", "aria-label=\"Halt\""))
        reason = patch.fits(self.root)
        self.assertIn(patch.SESSION, reason)
        with self.assertRaisesRegex(ValueError, "chatgpt-session"):
            patch.apply(self.root)
        for name, data in self.originals.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_a_tree_where_only_some_files_are_patched_is_refused(self):
        (self.root / patch.SESSION).write_bytes(patch.patch_source(SESSION, patch.EDITS[patch.SESSION]).encode("utf-8"))
        self.assertEqual(patch.status(self.root), "partial")
        with self.assertRaisesRegex(RuntimeError, "revert"):
            patch.apply(self.root)

    def test_a_missing_tree_is_reported(self):
        self.assertEqual(patch.status(self.root / "nowhere"), "missing")
        self.assertEqual(patch.apply(self.root / "nowhere"), "missing")

    def test_fits_is_quiet_when_the_anchors_are_there(self):
        self.assertIsNone(patch.fits(self.root))
        patch.apply(self.root)
        self.assertIsNone(patch.fits(self.root))

    def test_stop_icon_selector_variant_is_supported(self):
        icon_session = (
            'const CHATGPT_STOP_ICON_BUTTON_SELECTOR = `form[data-chatgpt-composer] '
            'button[type="button"]:has(svg path[d="${CHATGPT_STOP_ICON_PATH}"])`;\n'
        )
        self.put(patch.SESSION, icon_session)
        self.assertIsNone(patch.fits(self.root))
        self.assertEqual(patch.apply(self.root), "patched")
        self.assertIn('[aria-label="Stop"]', self.text(patch.SESSION))
        self.assertIn('[aria-label="停止"]', self.text(patch.SESSION))
        self.assertEqual(patch.revert(self.root), "original")
        self.assertEqual(self.text(patch.SESSION), icon_session)


if __name__ == "__main__":
    unittest.main()
