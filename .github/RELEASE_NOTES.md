AgentCompass v0.3.17 keeps the hidden tray window's memory small. It builds on v0.3.16.

- **The hidden window uses about 7 MB of RAM again**: in v0.3.16 it could stay at about 40 MB.
  - **Cause**: the window gave its memory back to Windows only once, right after building its pages. Data that loaded after that filled the pages and pulled the memory back in.
  - **Fix**: whenever the window is hidden and nothing is loading, it gives its memory back again.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.17/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
