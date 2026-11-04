AgentCompass v0.3.8 is a fix release on top of v0.3.7.

- **Offline routing no longer misreads code work**: When the System-One scorer is unavailable, the offline fallback no longer treats device nouns as computer use. "Fix the keyboard shortcuts in editor.py" and "Fix mouse event handler in app.ts" now route as debugging. A named source file or a debugging word also keeps "translation" work out of text translation, so "Fix translation.py", "Debug translation pipeline" and "修复翻译模块的 bug" route as debugging too.
- **Raised floors grade from the floor**: If you raise an ability floor above the shipped score origin (for example in the tray's ability-floor chart), a model that only just clears it no longer earns a free share of the ability score.
- **Plugin updates survive re-pointed upstream tags**: The tray's plugin updater takes upstream tags (`git fetch --tags --force`). A clone that kept a release tag from before upstream rewrote its history no longer fails every update with "would clobber existing tag".

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.8/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
