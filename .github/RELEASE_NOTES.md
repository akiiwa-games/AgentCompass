AgentCompass v0.3.15 makes the tray window switch pages without freezing, stops the installed tray from opening console windows, and gives it a Start menu entry. It builds on v0.3.14.

- **Page switches no longer freeze the window**: before, every click on a page or settings tab froze the window for 0.2-0.9 s, and a page's first visit took 0.6-1.2 s. Now switching takes about 0.05-0.2 s, and the first visit is just as fast.
  - **Cause**: a switch hid one page and showed another, so every native window on the page coming back was placed and shown again. On machines with global window hooks (input methods, security and remote-access agents), each of those Windows calls costs about a millisecond.
  - **Fix**: hidden pages now keep their layout and only their window is hidden. Pages you haven't opened yet are built while the window is idle.
- **No more console windows from the tray**: opening the tray window (it lists models and checks CLI versions) could flash a row of terminal windows.
  - **Cause**: the installed tray runs each command through a small helper, and that helper started the command without asking Windows to hide its console.
  - **Fix**: the helper now starts every command without a window. The Windows Sandbox check runs a console command through the installed tray and fails if any console window appears.
- **A Start menu entry for the tray**: the installer adds "AgentCompass" to the Start menu, and can add a desktop icon if you tick that option. If the tray is already running, opening the entry brings up its window; before, a second launch did nothing.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.15/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
