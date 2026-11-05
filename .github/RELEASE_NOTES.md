AgentCompass v0.3.16 makes the tray window open almost instantly. It builds on v0.3.15.

- **The window opens in about 60 ms**: before, opening it took 1-1.5 s, and for a few seconds afterwards building the other pages froze it for up to a second at a time.
  - **Cause**: every open started a new process, which built the window and then each page from scratch. On machines with global window hooks (input methods, security and remote-access agents), every native window costs about a millisecond, and the pages built in the background also read account files and ran CLI version checks on the window's own thread.
  - **Fix**: the tray now starts the window hidden 8 s after it starts and builds every page ahead of time. Opening the window only shows it, and closing it only hides it. When the window is shown again, each page reloads its data. Pages built in the background read their data on a separate thread.
  - **Memory**: while hidden, the window hands its memory back to Windows and uses about 7 MB of RAM. It exits when the tray exits.
- **Test runs no longer write to the real tray log**: the tray's log path ignored the test isolation, so every test run added "restart_tray skipped" lines to `~/.agent-compass/tray.log`. Those lines looked like failed restarts.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.16/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
