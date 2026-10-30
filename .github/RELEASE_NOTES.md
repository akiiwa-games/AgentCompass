AgentCompass v0.3.1 adds an animated Decision Compass: the Windows tray now replays each routing decision from task to winner, and the router gains verified fast-mode launch contracts.

- Decision Compass decision theatre: an animated replay of task → work kind → candidate routes → ranking → top-two capability radar → winner with its effort. Ledger entries keep decision snapshots, so past decisions replay faithfully.
- New radar rendering with unified capability axes and four editable model ratings, plus a verdict strip that summarises each decision in one line.
- Model, bundle and work-kind names are short and consistent across the page (theatre, verdict strip, recent decisions, candidates, radar pickers, status line and Quota Monitor). Raw ids stay in tooltips.
- Fast-mode launch contracts for Codex and Claude: fast keeps model and effort, normal launches pass the off value explicitly, and Claude fast mode needs an explicit paid opt-in.
- Effort routing is reset-aware: observed and expected reset horizons feed dynamic urgency, and task depth is matched to model-specific ability and burn curves.
- Host-specific defaults are gone: the Grok2API integration and its port default are retired, and the reference catalogue and fixtures are realigned with the neutral fleet.
- The README hero is now a single animated Decision Compass replay rendered from the real window with synthetic data.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.1/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
