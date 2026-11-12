AgentCompass v0.4.6 scores every model on four categories and on quota economy, and nothing else. It builds on v0.4.5.

- **One scoring.**
  - The classifier states how much the task needs (0–3) of four categories: creativity (`divergence`), coding, aesthetics and tool operation.
  - The router fits each candidate's category scores to that need, capped at the task's target, and subtracts the quota economy: the part of the task's spend that lands over the recovery line of the account's quota window.
  - Equal net scores go to the candidate that starts sooner, then finishes sooner, then costs fewer list-price USD.
  - The old difficulty floors, per-model rows, tier gates and exchange-rate pricing are removed. There is no second path.
- **Public data only.**
  - Category scores come from cited public boards on one panel scale. A missing score is converted from the model's same-series sibling or bridged from its arena.ai Overall tier, and marked as estimated.
  - Plan capacity comes from cited community data.
  - Maintainer scores ship in `references/user-scores.json`, and you can override them locally from the tray.
- **Plans are read from the account, never assumed.**
  - Antigravity reads the exact tier (Pro, Ultra 5x, Ultra 20x) from `loadCodeAssist` `paidTier.id`.
  - Grok reads its subscription tier from the Grok CLI endpoints. grok-bot's capacity is the Claude subscription at the same price, or the nearest one scaled by the price ratio.
  - An account whose plan cannot be read leaves automatic routing with the reason shown. There is no default plan.
- **`roll --workspace` takes a directory.**
  - One writer per directory on every host.
  - A request the host rejects stops the dispatch. It does not cool down or walk through healthy models.
  - Only a failure with evidence against a model or account cools it down and falls back.
- **Context rotation** follows `rotate_after_carried_tokens` from the tray settings.
- **Completed dispatches record ground truth:** active time, tokens and diff size.
- **Tray.**
  - A scores page for the four categories with manual scores.
  - Strategy presets 均衡 / 省额度 / 重置前蹬完.
  - Quota-line views for every priced decision.
  - English layout fixes.
- **Unoffered models.** A model the provider no longer offers is excluded on its own. Other models on the account stay available.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.6/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet for your own subscriptions.
