"""F11 probe: Oh My Pi OAuth subscription logins, run inside a foreign profile against the installed skill.

    python -X utf8 f11_omp_probe.py SCRIPTS_DIR {intact|broken} OUT.json

foreign_machine_check.verify_f11_omp_oauth_subscriptions runs it twice in the same throwaway profile. Every scenario
builds its own home under the profile's state directory: a synthetic ~/.omp/agent/agent.db with omp's table shape (no
real credential), a stub omp executable, a Paseo config with omp enabled and, where the scenario needs one, a native
Codex sign-in. `intact` builds each scenario as described. `broken` changes exactly the input each behaviour depends on
(the omp login signs in as another account, an API key is stored beside the login, the credential is enabled, ...), so
every check is shown to fail once against the behaviour it guards. omp itself is never run. Each check writes
{"ok": bool, "detail": ...}; details carry account names and flags only, never tokens, emails or account ids.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SCRIPTS, MODE, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
BROKEN = MODE == "broken"
sys.path.insert(0, str(SCRIPTS))

import allocator  # noqa: E402
import provider_inventory  # noqa: E402

STATE = Path(os.environ["ROUTER_STATE_DIR"])
HOMES = STATE / f"f11-homes-{MODE}"
NOW = dt.datetime.now(dt.timezone.utc)
SECRET, EMAIL = "SECRET-f11-omp-token", "omp-user@example.test"
checks: dict[str, dict] = {}


def check(name, ok, detail=None):
    checks[name] = {"ok": bool(ok), "detail": detail}


def home(name, *, codex_account=None, rows=(), models=None, raw_db=None):
    root = HOMES / name
    shutil.rmtree(root, ignore_errors=True)
    stub = root / ".bun" / "bin" / ("omp.exe" if os.name == "nt" else "omp")
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"")
    (root / ".paseo").mkdir()
    (root / ".paseo" / "config.json").write_text(json.dumps(
        {"agents": {"providers": {"codex": {"enabled": True}, "omp": {"enabled": True}}}}), encoding="utf-8")
    if codex_account:
        claim = {"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}
        jwt = "h." + base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("=") + ".s"
        (root / ".codex").mkdir()
        (root / ".codex" / "auth.json").write_text(json.dumps({"tokens": {
            "account_id": codex_account, "access_token": SECRET + "-native", "id_token": jwt}}), encoding="utf-8")
    agent = root / ".omp" / "agent"
    agent.mkdir(parents=True)
    if raw_db is not None:
        (agent / "agent.db").write_bytes(raw_db)
    else:
        with closing(sqlite3.connect(agent / "agent.db")) as db:
            db.execute("CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, "
                       "credential_type TEXT NOT NULL, data TEXT NOT NULL, disabled_cause TEXT DEFAULT NULL, "
                       "identity_key TEXT DEFAULT NULL)")
            db.executemany("INSERT INTO auth_credentials (provider, credential_type, data, disabled_cause) VALUES (?,?,?,?)",
                           [(p, t, json.dumps(d), cause) for p, t, d, cause in rows])
            db.commit()
    if models:
        with closing(sqlite3.connect(agent / "models.db")) as db:
            db.execute("CREATE TABLE model_cache (provider_id TEXT PRIMARY KEY, version INTEGER, models TEXT)")
            db.executemany("INSERT INTO model_cache VALUES (?,?,?)",
                           [(pid, 1, json.dumps([{"id": m} for m in ids])) for pid, ids in models.items()])
            db.commit()
    return root


def login(account, provider="openai-codex", disabled=None):
    return (provider, "oauth", {"access": SECRET, "refresh": "r", "expires": 0, "accountId": account, "email": EMAIL},
            disabled)


def discover(root):
    return provider_inventory.discover(root, {"PATH": ""}, NOW)


def applied(snapshot):
    return provider_inventory.apply(allocator.load_accounts(allocator.reference_fleet_path()), snapshot)


def omp_rows(snapshot):
    return {n: r for n, r in snapshot["accounts"].items() if n.startswith("omp-")}


def safe(row):
    return {k: row.get(k) for k in ("family", "quota_family", "billing", "status", "quota_account", "plan")} if row else None


# 1. A login on the native Codex account shares that account's quota as a subscription; no metered `omp` side.
shared = home("shared", codex_account="shared-acct",
              rows=[login("other-acct" if BROKEN else "shared-acct")],
              models={"openai-codex:1": [b.get("launch_model") or b["model"] for b in (allocator.load_accounts(allocator.reference_fleet_path())
                                                              ["accounts"].get("codex") or {}).get("bundles", {}).values()]})
snap = discover(shared)
row = snap["accounts"].get("omp-openai-codex")
check("an omp OAuth login signed in as the native Codex account is a subscription sharing its quota",
      row and row.get("billing") == "subscription" and row.get("quota_account") == "codex"
      and row.get("identity") == snap["accounts"]["codex"].get("identity"), safe(row))
dumped = json.dumps(snap) if not BROKEN else json.dumps(snap) + SECRET   # broken: a dump carrying the raw credential
check("discovery output carries no omp token or email", SECRET not in dumped and EMAIL not in dumped)
accounts = applied(snap)
bundles = allocator.bundles(accounts)
omp = {bid: b for bid, b in bundles.items() if b.get("harness") == "omp"}
check("omp's bundles sit on the Codex account with omp's `<provider>/<model>` launch ids and no twin",
      omp and all(b["account"] == "codex" and str(b.get("launch_model", "")).startswith("openai-codex/") for b in omp.values())
      and (lambda keys: len(keys) == len(set(keys)))(
          [(allocator.bundle_harness(b, "codex", bid), allocator.canonical(b["model"], accounts), b.get("effort"))
           for bid, b in bundles.items() if b["account"] == "codex"]),
      sorted({b["account"] for b in omp.values()}))

# 2. The plan treats an omp bundle like Pi's: eligible, drawing the Codex window, out when that window is empty.
if omp:
    accounts["accounts"].setdefault("codex", {})["verified"] = True
    omp_id = sorted(omp)[0]
    task = {"objective": "Fix a bug in parser.py", "work_kind": "general_implementation", "expected_hours": 1,
            "constraints": {"model": bundles[omp_id]["model"]}}

    def usage(rem):
        return {"read_ok": True, "accounts": {"codex": {"windows": [{
            "id": "usage", "remaining_pct_before_reset": rem, "observed_remaining_pct": rem, "seconds_until_reset": 100 * 3600,
            "resets_at": (NOW + dt.timedelta(hours=100)).isoformat()}]}}}

    def ranked(rem):
        plan = allocator.plan(task, usage(rem), NOW, accounts=accounts, overrides=[], records=[], host_policy_config=None)
        return next((c for c in plan["ranking"] if c["id"] == omp_id), {})

    full, empty = ranked(80), ranked(0)
    check("an omp subscription bundle is eligible and draws the shared Codex window, out when it is empty",
          full.get("excluded") == [] and (full.get("capacity") or {}).get("window") == "usage" and empty.get("excluded"),
          {"full": full.get("excluded"), "window": (full.get("capacity") or {}).get("window"), "empty": empty.get("excluded")})
else:
    check("an omp subscription bundle is eligible and draws the shared Codex window, out when it is empty", False,
          "no omp bundle")
subs_root = home("subscriptions-only", codex_account="shared-acct", rows=[login("shared-acct")])
if BROKEN:   # an omp models.yml is the API-key side
    (subs_root / ".omp" / "agent" / "models.yml").write_text("providers: {}\n", encoding="utf-8")
subs = discover(subs_root)
check("with only subscription logins there is no metered `omp` account, in discovery or routing",
      "omp" not in subs["accounts"] and "omp" not in applied(subs)["accounts"], safe(subs["accounts"].get("omp")))

# 3. Stored API keys keep the metered side; with a login beside one, both are reported.
keyed = discover(home("api-key", rows=[login("acct")] + [("openai", "api_key", {"key": SECRET}, None)] * (0 if BROKEN else 1)))
metered = keyed["accounts"].get("omp")
check("a stored omp API key keeps the metered `omp` account beside the subscription login",
      metered and metered.get("billing") == "metered" and metered.get("status") == "ready"
      and "omp-openai-codex" in keyed["accounts"], safe(metered))

# 4. An omp-only login is its own subscription account on the Codex plan defaults.
only = discover(home("omp-only", rows=[login("omp-only-acct", provider="openai-codex-unmapped" if BROKEN else "openai-codex")]))
row = only["accounts"].get("omp-openai-codex")
spec = applied(only)["accounts"].get("omp-openai-codex") if row else None
check("an omp-only OAuth login is its own subscription account with the Codex plan's windows and omp bundles",
      row and row.get("quota_account") in (None, "omp-openai-codex") and spec and spec.get("billing") == "subscription"
      and spec.get("windows") and spec.get("bundles")
      and all(b.get("harness") == "omp" and b.get("provider") == "omp" for b in spec["bundles"].values()),
      {"row": safe(row), "bundles": len((spec or {}).get("bundles") or {})})

# 5. A disabled credential is ignored.
disabled = discover(home("disabled", codex_account="shared-acct",
                         rows=[login("shared-acct", disabled=None if BROKEN else "invalid_grant")]))
check("a disabled omp credential is ignored", not omp_rows(disabled), sorted(omp_rows(disabled)))

# 6. Two logins of one provider are two accounts.
pair = discover(home("pair", codex_account="shared-acct",
                     rows=[login("shared-acct"), login("shared-acct" if BROKEN else "second-acct")]))
first, second = pair["accounts"].get("omp-openai-codex"), pair["accounts"].get("omp-openai-codex-2")
check("two omp logins of one provider are told apart",
      first and second and first.get("quota_account") == "codex" and second.get("quota_account") != "codex"
      and first.get("identity") != second.get("identity"), {"first": safe(first), "second": safe(second)})

# 7. A corrupt or locked agent.db is missing_credentials, not a crash.
outcomes = {}
corrupt_root = home("corrupt", raw_db=None if BROKEN else b"not a sqlite database" * 64, rows=[login("acct")])
locked_root = home("locked", rows=[login("acct")])
for label, root in (("corrupt", corrupt_root), ("locked", locked_root)):
    try:
        if label == "locked" and not BROKEN:
            with closing(sqlite3.connect(root / ".omp" / "agent" / "agent.db")) as holder:
                holder.execute("BEGIN EXCLUSIVE")
                result = discover(root)
                holder.rollback()
        else:
            result = discover(root)
        outcomes[label] = ((result["accounts"].get("omp") or {}).get("status"), sorted(omp_rows(result)))
    except Exception as exc:   # a crash is the failure this check guards
        outcomes[label] = ("crash", type(exc).__name__)
check("a corrupt or locked omp agent.db is missing_credentials with no login, not a crash",
      all(status == "missing_credentials" and not names for status, names in outcomes.values()), outcomes)

shutil.rmtree(HOMES, ignore_errors=True)
OUT.write_text(json.dumps(checks, indent=2, default=str), encoding="utf-8")
