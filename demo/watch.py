#!/usr/bin/env python3
"""WATCH & ALERT — nightly insolvency-distress monitor for The Company Brain.

Polls Orbital (TaxiQL) for the insolvency profile of a watchlist of UK company
numbers, diffs each against a saved snapshot (``demo/watch_state.json``), and
emits a clear digest of *what changed* — the basis of a nightly alert email /
Slack / Discord post.

It watches four signals that an analyst would want paged about:

  * distress-stage    — e.g. "No insolvency on record" -> "Winding-up petition"
  * latest-event      — the underlying Companies House filing changed
  * controller-risk   — a director now linked to other distressed companies
  * contract-at-risk  — the top public-sector contract on the company changed

Pure Python stdlib — no dependencies. Same TaxiQL contract the demo backend uses.

Usage
-----
    python3 demo/watch.py                 # poll watchlist, diff vs snapshot
    python3 demo/watch.py --seed          # (re)seed snapshot, no alerts
    python3 demo/watch.py 07021926 ...    # override the watchlist
    python3 demo/watch.py --json          # machine-readable digest (for sinks)
    python3 demo/watch.py --narrative     # enrich alerts via llm service :8903
    python3 demo/watch.py --state PATH    # use an alternate snapshot file

Exit code is 2 when alerts fired, 0 when nothing changed / first-run seed, 1 on
error. That lets cron / CI gate a notification step on "did anything change".
"""
import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ORBITAL = os.environ.get("ORBITAL_URL", "http://localhost:9022") + "/api/taxiql"
LLM = os.environ.get("LLM_URL", "http://localhost:8903")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "watch_state.json")

# Default watchlist — a spread of distress stages so the demo shows real signal.
DEFAULT_WATCHLIST = [
    "07021926",  # AASEYA — winding-up petition
    "08810995",  # CAVENDISH WOOD — in liquidation
    "10976115",  # OFFICE TEK — clean, but controller runs 145 distressed cos
    "04151059",  # BIG IDEAS GROUP — clean
    "04088165",  # MUTUAL VISION TECHNOLOGIES — clean
]

# The fields we snapshot per company. Each maps to an alert category below.
WATCHED_FIELDS = (
    "companyName",
    "stage",
    "latestEvent",
    "eventDate",
    "controllerRisk",
    "topContractBuyer",
    "topContractTitle",
)


def taxiql(query, timeout=120):
    """POST a TaxiQL query to Orbital and return the decoded JSON body."""
    req = urllib.request.Request(
        ORBITAL,
        data=query.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_profile(company_number):
    """Return the watched subset of a company's InsolvencyProfile.

    Whitelists the company number so it can't break out of the TaxiQL string
    literal (Companies House numbers are 8 alphanumerics).
    """
    cid = "".join(c for c in (company_number or "") if c.isalnum())[:10]
    query = (
        'given { id : uk.gov.CompanyRegistrationNumber = "%s" }\n'
        "find { brain.insolvency.InsolvencyProfile }" % cid
    )
    raw = taxiql(query, timeout=60) or {}
    snap = {k: raw.get(k) for k in WATCHED_FIELDS}
    snap["companyNumber"] = cid
    return snap


def _contract(profile):
    """Human-readable 'top contract' label, or None if there isn't one."""
    buyer = profile.get("topContractBuyer")
    title = profile.get("topContractTitle")
    if not buyer and not title:
        return None
    return " / ".join(p for p in (title, buyer) if p)


def diff_company(old, new):
    """Compare two snapshots; return a list of {category, ...} change records."""
    changes = []
    name = new.get("companyName") or old.get("companyName") or new.get("companyNumber")

    # 1) distress-stage transition (the headline signal)
    if (old.get("stage") or None) != (new.get("stage") or None):
        changes.append({
            "category": "distress-stage",
            "company": name,
            "from": old.get("stage"),
            "to": new.get("stage"),
        })

    # 2) underlying filing/event moved (catches re-filings within a stage)
    if (old.get("latestEvent") or None) != (new.get("latestEvent") or None) or \
       (old.get("eventDate") or None) != (new.get("eventDate") or None):
        changes.append({
            "category": "latest-event",
            "company": name,
            "from": _fmt_event(old),
            "to": _fmt_event(new),
        })

    # 3) controller-risk changed (director now linked to distressed companies)
    if (old.get("controllerRisk") or None) != (new.get("controllerRisk") or None):
        changes.append({
            "category": "controller-risk",
            "company": name,
            "from": old.get("controllerRisk"),
            "to": new.get("controllerRisk"),
        })

    # 4) contract-at-risk — the top public-sector contract changed
    if _contract(old) != _contract(new):
        changes.append({
            "category": "contract-at-risk",
            "company": name,
            "from": _contract(old),
            "to": _contract(new),
        })

    return changes


def _fmt_event(profile):
    ev = profile.get("latestEvent")
    if not ev:
        return None
    date = profile.get("eventDate")
    return "%s (%s)" % (ev, date) if date else ev


def narrative(name):
    """Optional one-line analyst gloss from the llm service; '' on failure."""
    try:
        url = LLM + "/narrative?name=" + urllib.parse.quote(name or "")
        with urllib.request.urlopen(url, timeout=20) as r:
            return (json.loads(r.read().decode("utf-8")) or {}).get("summary", "")
    except Exception:
        return ""


def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("companies", {})
    except (OSError, ValueError):
        return {}


def save_state(path, companies):
    payload = {
        "updated": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "source": ORBITAL,
        "companies": companies,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


# ── presentation ──────────────────────────────────────────────────────────────
_ARROW = "->"
_ICON = {
    "distress-stage": "[STAGE]   ",
    "latest-event": "[EVENT]   ",
    "controller-risk": "[CONTROL] ",
    "contract-at-risk": "[CONTRACT]",
}


def _val(v):
    return repr(v) if v is None else '"%s"' % v


def render_text(alerts, errors, seeded, narrate=False):
    out = []
    stamp = _dt.datetime.now().replace(microsecond=0).isoformat()
    out.append("=" * 68)
    out.append("  COMPANY BRAIN — WATCH & ALERT DIGEST   %s" % stamp)
    out.append("=" * 68)

    if seeded:
        out.append("")
        out.append("  First run: seeded snapshot for %d compan%s. No alerts yet."
                   % (seeded, "y" if seeded == 1 else "ies"))

    if not alerts and not seeded:
        out.append("")
        out.append("  No changes. Watchlist quiet across all monitored companies.")

    if alerts:
        out.append("")
        out.append("  %d ALERT%s across %d compan%s:" % (
            len(alerts), "" if len(alerts) == 1 else "S",
            len({a["company"] for a in alerts}),
            "y" if len({a["company"] for a in alerts}) == 1 else "ies"))
        # Group by company for a readable digest.
        for company in dict.fromkeys(a["company"] for a in alerts):
            out.append("")
            out.append("  * %s" % company)
            for a in [x for x in alerts if x["company"] == company]:
                out.append("      %s %s" % (_ICON[a["category"]], a["category"]))
                out.append("                  %s %s %s"
                           % (_val(a["from"]), _ARROW, _val(a["to"])))
            if narrate:
                g = narrative(company)
                if g:
                    out.append("      note: %s" % g)

    if errors:
        out.append("")
        out.append("  %d company could not be polled:" % len(errors))
        for cid, msg in errors:
            out.append("      ! %s — %s" % (cid, msg))

    out.append("")
    out.append("-" * 68)
    verdict = "ALERTS FIRED" if alerts else ("SEEDED" if seeded else "ALL QUIET")
    out.append("  %s | alerts %d | errors %d" % (verdict, len(alerts), len(errors)))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Watch & alert on insolvency distress changes.")
    ap.add_argument("companies", nargs="*", help="company numbers to watch (default: built-in watchlist)")
    ap.add_argument("--seed", action="store_true", help="(re)seed snapshot from live data, suppress alerts")
    ap.add_argument("--json", action="store_true", help="emit machine-readable digest for a sink")
    ap.add_argument("--narrative", action="store_true", help="enrich alerts with llm service one-liners")
    ap.add_argument("--state", default=STATE_FILE, help="snapshot file (default: demo/watch_state.json)")
    args = ap.parse_args(argv)

    watchlist = args.companies or DEFAULT_WATCHLIST
    prior = load_state(args.state)
    first_run = not prior

    current, alerts, errors, seeded = {}, [], [], 0
    for cid in watchlist:
        try:
            profile = fetch_profile(cid)
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append((cid, str(e)))
            # keep any prior snapshot for this company so we don't lose state
            if cid in prior:
                current[cid] = prior[cid]
            continue

        current[cid] = profile
        if first_run or args.seed:
            seeded += 1
            continue
        old = prior.get(cid)
        if old is None:
            # newly added to the watchlist — seed it silently, no false alert
            seeded += 1
            continue
        alerts.extend(diff_company(old, profile))

    # Persist snapshot unless we're only diffing without wanting to advance state.
    # We always advance state so the *next* run diffs against the latest world.
    save_state(args.state, current)

    if args.json:
        print(json.dumps({
            "generated": _dt.datetime.now().replace(microsecond=0).isoformat(),
            "alerts": alerts,
            "errors": [{"company": c, "error": m} for c, m in errors],
            "seeded": seeded,
            "fired": bool(alerts),
        }, indent=2))
    else:
        print(render_text(alerts, errors, seeded, narrate=args.narrative))

    if errors and not alerts:
        return 1
    return 2 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
