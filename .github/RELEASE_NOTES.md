AgentCompass v0.4.3 fixes how failed usage readings are retried and shown, and repairs the tray's quota, integrations and Scoring & Capabilities pages. It builds on v0.4.2.

- **Failed usage readings retry sooner, without hammering the API**: after a failure the collector retries in 2 minutes, doubling up to the normal poll interval, and goes back to the normal interval after a success. Changing an account's credentials resets the count. `agentcompass usage --force` reads now but still honours Claude's 429 backoff.
- **The tray says why an update needs the full installer**, and a plan refused with 402 shows when it will be checked again.
- **Quota page**:
  - It no longer shows readings from when the tray started. Opening or switching to the page reloads the latest local readings, and the page reloads every 15 seconds while you look at it. None of this sends extra usage requests.
  - Bars show **remaining** quota, the same as usage-float: red at 10% or less, amber at 30% or less.
  - Each card shows when its reading was taken. A failed reading is marked "reading failed" with the old reading's time. A reading that is merely old but still available stays "ready" with a greyed bar.
- **Integrations page**:
  - A checkbox no longer flips back while the page is refreshing, and quick clicks no longer overwrite each other. If the settings file can't be written, the tray says so and puts the checkbox back.
  - Components that aren't downloaded show "not installed". Enabling Gemini Web deploys it, and ticking autostart starts it.
  - After "Check for updates", the badge comes from each component's result: "up to date" only when everything is current, and "partly failed", "needs attention" (a newer release, or the release check failed) or "updating" otherwise.
  - Action results such as restart or Grok session check stay visible. Log buttons open the right file. A failed EasyTier peer read shows as such.
- **Scoring & Capabilities page**: the ability chart now follows the display scale. It used to draw at 100%, so its text was about a third of the panel's on high-DPI screens. Cards get titles and more spacing, and the missing English labels are added.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.3/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
