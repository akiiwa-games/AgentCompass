AgentCompass v0.4.6 changes how a model without its own leaderboard data gets its routing scores. A model with a measured model in the same series now takes its scores and price from that model, converted by public data both of them have. No defaults or fallbacks are left for such a model. It builds on v0.4.5.

- **Same-series conversion is the one way an unmeasured model is scored.**
  - The base is the same-series model that has a direct measurement for that score. Among several, the base is the one nearest on a public reading both models share: first the Artificial Analysis Intelligence Index, then the shared leaderboard readings. Tier is used only when they share no reading at all.
  - Scores move from the base along the existing calibration fit, using the shared reading, and stay inside the measured range.
  - The price per hour is the base's price times the ratio of their API list prices, weighted by the cited community token mix (cache reads included).
  - Series membership comes from the existing model-family resolution. Models with no measured sibling keep a skill-checked public-data estimate, and only where one exists.
  - A direct measurement replaces a converted value on the next data refresh. An edited value is never overwritten.
  - Converted and estimated scores are marked in the capability catalog (`~`), in routing notes and in the tray data, and each one records its base, reading and fit.
  - The old family shrink (tier −0.3, scores ×0.9) and the flat price for an unknown family member are removed.
- **Claude Fable 5.2** now takes its scores and price from Fable 5.1.
  - Its price per hour is 14.96 USD, down from a 72.92 fallback.
  - With both Fable models enabled, it ranks above Fable 5.1 on reasoning work and level with it on coding.
  - The v0.4.5 note that it was not routed automatically held only for the demo fleet.
- **Other models whose scores changed:** Claude Opus 4.7/4.8, Gemini 4 Carbon, Gemini 3.1 Pro Preview, the Gemini Flash rows, Grok 4.5/4.6, Kimi K2.6/K2.7, Qwen3-Max, Qwen3.6-Plus, Qwen3.7-Max and gpt-oss-120b.
  - Visual and GUI values only went up, because the sibling's value is no longer shrunk.
  - Qwen3-Max's coding score now follows the measured Qwen3.6-Plus.
- **Five-axis data** (still off by default) uses the same conversion for missing components and prices.
- **Public data.** Installs that receive public routing data get the new scores from GitHub without updating. This release ships them in the installer as well.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.6/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
