AgentCompass v0.3.10 lets a machine run entirely from the released product, with no development checkout, on top of v0.3.9.

- **EasyTier has a per-machine home**: The node lives in `%ProgramData%\AgentCompass\easytier`. That is outside any user profile and outside the installer's app directory, which a reinstall replaces. `easytier_setup.py install --migrate-from <old dir>` moves an existing node there and re-points the scheduled task. It rolls back if the new node isn't healthy, and `--dry-run` previews it without elevation. The tray falls back to the old checkout location while it still exists.
- **Moving plugins out of a checkout**: `python tray/deploy.py migrate-plugins [--dry-run]` moves the Usage Float, ChatGPT Web, Gemini Web and Dot2API clones into `~/.agent-compass/plugins/`. Local patches and untracked files come along, and junctions that point back at the checkout are replaced. Dot2API's virtual environment is rebuilt, and whatever was running is restarted. You can re-run or resume it. See "Moving from a checkout to the product" in the README.
- **Wheel upgrades keep their runtime pieces**: The Grok Bot CLI runtime and the Paseo bridge's `node_modules` now live under `~/.agent-compass/` instead of `site-packages`, so a pip or uv upgrade no longer wipes them.
- **Packaged tray fixes**: `deploy.py check` / `install` no longer crash in the installed tray, and the plugin cards show each plugin's real location.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.10/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
