AgentCompass v0.4.0 makes updates small: an installed copy now downloads only our code (about 0.8 MB) and reinstalls the full package only when the runtime underneath it changes. It builds on v0.3.18.

- **Updates download a code package, not the whole installer**: every update used to download and silently run the full 25-35 MB installer, even when only our Python code had changed.
  - **How it works**: the installer now carries only the runtime (Python, Tk, third-party libraries and a thin loader). Our code ships as `app-<version>.zip` and is loaded from `app\current`; the previous version stays in `app\previous`. Each release publishes `manifest.json` with a runtime fingerprint; when it matches the installed runtime, the update downloads only the code package, checks its sha256 and swaps it in.
  - **When the full installer is still used**: a change to Python, PyInstaller or the loader; a native third-party library added, removed or upgraded; a change to the installer's actions (`.iss`) or the packaging spec (`.spec`); or a code package that needs a newer runtime than the one installed. See the README for the full rules.
  - **Upgrading from v0.3.18 or earlier**: older clients only know the full installer, so this one update still downloads it. Updates after v0.4.0 use code packages.
- **A broken code package rolls back by itself**: if a new code package fails to start, the tray, its components and the command line return to the previous version and start again in a fresh process.
- **An open panel no longer blocks the update**: an update leaves an open panel alone and tells you to reopen it for the new version; the tray opens the new panel at once.
- **No more false "command-line version mismatch"**: the installed version is read from the code package, so the tray no longer reads it as `0.0.0`.
- **The installer is smaller**: 25 MB instead of 30 MB; libraries that happened to be on the build machine (numpy, Qt bindings) are no longer bundled.
- **A review goes to a different worker**: a brief that reviews work is never resumed on the worker that wrote that work.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.0/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
