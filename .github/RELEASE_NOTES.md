AgentCompass v0.3.18 keeps Paseo dispatches from stalling on stale workers and from recording finished work as canceled. It builds on v0.3.17.

- **A follow-up no longer stalls on an old, closed worker**: a roll could pick an idle worker from two weeks earlier, or one that had done unrelated work, and the turn never started. The dispatch then waited out its 10-minute lease, and the parent had to roll again.
  - **Cause**: a worker counted as reusable whenever it shared the bundle, even when Paseo had already collected its runtime (`closed`). Restoring that worker took about 25 s, longer than the 25 s the bridge call was allowed. The new dispatch labels were written only after the send, so the cut-off call left the worker bound to its old dispatch and the router could never observe it.
  - **Fix**: closed workers, workers idle for more than 24 h, and workers whose brief shares no words with the new one are no longer resumed. A resume writes the labels before it sends and may take up to 90 s. A send the host refuses gives the worker its old labels back and starts a fresh worker.
- **Finished work is recorded as completed**: a worker that finished but was observed late (an hour after Paseo collected its idle runtime, or after a plugin reload) was recorded as canceled or failed.
  - **Fix**: the notification watcher keeps its first sighting of the finish, and a later `closed`, `error` or archived state no longer overrides it.
- **A provider that refuses to start a worker cools down**: after Paseo refused to create a worker (for example `Timed out refreshing Claude 2`), the next roll picked the same account again 17 s later.
  - **Fix**: routing skips that account (provider failures) or model (other failures) for 15 min, doubling with each consecutive failure up to 2 h. A worker that starts on the account clears the cooldown. The failure class, never the provider's error text, is recorded in the dispatch events.
- **Magpie pools count down to the next refill**: a pool card counts down to the soonest reset of an account that has spent quota, and its hover lists each account's remaining quota and reset.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.18/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
