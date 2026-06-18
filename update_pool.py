#!/usr/bin/env python3
"""
World Cup Pool — standings updater.

What it does, each time you run it:
  1. Fetches the latest official World Cup results (machine-readable feed).
  2. Compares them against the last snapshot it saved (results_snapshot.json).
  3. Reports exactly what changed since last run (new results, corrections).
  4. Recomputes every owner's pool total using your scoring rules.
  5. Regenerates world-cup-pool.html for you to re-upload.

Run it:  python3 update_pool.py

Why this feed instead of fifa.com directly:
  FIFA's standings page is rendered in-browser and has no public data API,
  so it can't be read reliably by a script. This feed (openfootball, public
  domain, no API key) tracks the same official results in JSON form. Swap
  RESULTS_URL if you ever move to a paid FIFA-direct provider.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

RESULTS_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(HERE, "results_snapshot.json")
PAGE_PATH = os.path.join(HERE, "world-cup-pool.html")
TEMPLATE_PATH = os.path.join(HERE, "page_template.html")

# Pool scoring: points = country PR * multiplier for the stage/outcome.
STAGE_MULT = {
    "group_win": 1, "group_draw": 0.5,
    "r32": 3, "r16": 5, "qf": 7, "sf": 9, "third": 11, "final": 15,
}

# Draft: owner -> [(country, power ranking), ...]
ROSTER = {
    "SEA RANCH": [("Norway", 9), ("Croatia", 17), ("Austria", 23), ("Sweden", 21), ("DR Congo", 36), ("Curacao", 45)],
    "LOS ANGELES": [("South Korea", 31), ("Morocco", 12), ("Portugal", 6), ("Bosnia and Herzegovina", 32), ("Algeria", 30), ("New Zealand", 41)],
    "SEMINARY": [("Germany", 7), ("Argentina", 5), ("Uruguay", 12), ("Scotland", 24), ("Australia", 34), ("Iraq", 41)],
    "DUBLIN": [("Iran", 35), ("Japan", 15), ("Senegal", 20), ("Ivory Coast", 26), ("Panama", 36), ("Haiti", 45)],
    "SAN LUIS OBISPO": [("Canada", 25), ("Ecuador", 17), ("Brazil", 4), ("Paraguay", 28), ("South Africa", 36), ("Saudi Arabia", 36)],
    "PINE STREET": [("Mexico", 19), ("Colombia", 11), ("Switzerland", 15), ("Ghana", 28), ("Spain", 1), ("Uzbekistan", 41)],
    "BOISE": [("USA", 14), ("Netherlands", 8), ("Czechia", 26), ("France", 1), ("Qatar", 41), ("Jordan", 45)],
    "KENTFIELD": [("Turkey", 21), ("Belgium", 9), ("Egypt", 28), ("England", 3), ("Tunisia", 32), ("Cape Verde", 36)],
}

# Feed spelling -> our roster spelling. Only differences need listing.
NAME_MAP = {
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Curaçao": "Curacao",
    "Czech Republic": "Czechia",
}

# Feed round label -> stage key. Group matchdays handled separately.
KNOCKOUT_STAGE = {
    "Round of 32": "r32",
    "Round of 16": "r16",
    "Quarter-final": "qf",
    "Semi-final": "sf",
    "Match for third place": "third",
    "Final": "final",
}


# ----------------------------------------------------------------------------
# CORE
# ----------------------------------------------------------------------------

def build_country_index():
    idx = {}
    for owner, countries in ROSTER.items():
        for name, pr in countries:
            idx[name] = {"owner": owner, "pr": pr}
    return idx


def norm(name):
    return NAME_MAP.get(name, name)


def fetch_results():
    req = urllib.request.Request(RESULTS_URL, headers={"User-Agent": "wc-pool-updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def is_group_round(round_label):
    return round_label.lower().startswith("matchday")


def extract_completed(feed):
    """Return a dict keyed by a stable match id -> normalized completed result."""
    out = {}
    for m in feed.get("matches", []):
        score = m.get("score") or {}
        ft = score.get("ft")
        if not ft or len(ft) != 2:
            continue  # not played yet
        t1, t2 = norm(m.get("team1", "")), norm(m.get("team2", ""))
        if not t1 or not t2:
            continue
        round_label = m.get("round", "")
        stage = "group" if is_group_round(round_label) else KNOCKOUT_STAGE.get(round_label)
        if stage is None:
            continue  # unknown round; skip rather than guess
        s1, s2 = ft[0], ft[1]
        # stable key: date + teams (teams alone could repeat across stages)
        key = f"{m.get('date','?')}|{t1}|{t2}|{stage}"
        out[key] = {
            "date": m.get("date"),
            "team1": t1, "team2": t2,
            "score1": s1, "score2": s2,
            "stage": stage,
            "round_label": round_label,
        }
    return out


def points_for(country, stage, outcome, country_idx):
    info = country_idx.get(country)
    if not info:
        return 0.0
    if stage == "group":
        key = "group_win" if outcome == "win" else "group_draw" if outcome == "draw" else None
    else:
        key = stage if outcome == "win" else None
    return info["pr"] * STAGE_MULT[key] if key else 0.0


def result_to_entries(match):
    """A match yields a per-country (country, stage, outcome) for both sides."""
    s1, s2 = match["score1"], match["score2"]
    if s1 > s2:
        o1, o2 = "win", "loss"
    elif s2 > s1:
        o1, o2 = "loss", "win"
    else:
        o1 = o2 = "draw"
    return [
        {"country": match["team1"], "stage": match["stage"], "outcome": o1},
        {"country": match["team2"], "stage": match["stage"], "outcome": o2},
    ]


def compute_totals(completed, country_idx):
    owner_totals = {o: 0.0 for o in ROSTER}
    country_totals = {c: 0.0 for c in country_idx}
    for match in completed.values():
        for e in result_to_entries(match):
            info = country_idx.get(e["country"])
            if not info:
                continue
            pts = points_for(e["country"], e["stage"], e["outcome"], country_idx)
            owner_totals[info["owner"]] += pts
            country_totals[e["country"]] += pts
    return owner_totals, country_totals


def diff_snapshots(old, new):
    """Report new matches and changed scores between two completed-result dicts."""
    added, changed = [], []
    for key, m in new.items():
        if key not in old:
            added.append(m)
        elif (old[key]["score1"], old[key]["score2"]) != (m["score1"], m["score2"]):
            changed.append((old[key], m))
    removed = [old[key] for key in old if key not in new]
    return added, changed, removed


def describe(match):
    return f"{match['team1']} {match['score1']}-{match['score2']} {match['team2']} ({match['round_label']})"


# ----------------------------------------------------------------------------
# PAGE GENERATION
# ----------------------------------------------------------------------------

def to_result_log(completed):
    """Flatten to the per-country entry list the HTML page expects."""
    log = []
    for m in completed.values():
        log.extend(result_to_entries(m))
    return log


def build_seed(completed, country_idx):
    return {
        "countryOwnerPr": {c: {"owner": i["owner"], "pr": i["pr"]} for c, i in country_idx.items()},
        "results": to_result_log(completed),
    }


def regenerate_page(completed, country_idx):
    if not os.path.exists(TEMPLATE_PATH):
        print(f"  ! template not found at {TEMPLATE_PATH}; skipping page rebuild.")
        return False
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    seed = build_seed(completed, country_idx)
    updated = datetime.now(timezone.utc).strftime("%d %B %Y")
    html = html.replace("__SEED_JSON__", json.dumps(seed))
    html = html.replace("__UPDATED__", updated)
    with open(PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return True


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    country_idx = build_country_index()

    print("Fetching latest results ...")
    try:
        feed = fetch_results()
    except Exception as e:
        print(f"  ! could not reach the results feed: {e}")
        sys.exit(1)

    new_completed = extract_completed(feed)
    print(f"  {len(new_completed)} completed matches in the feed.\n")

    old_completed = {}
    if os.path.exists(SNAPSHOT_PATH):
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            old_completed = json.load(f)

    added, changed, removed = diff_snapshots(old_completed, new_completed)

    if not old_completed:
        print("First run — establishing a baseline. No prior snapshot to compare against.")
    elif not (added or changed or removed):
        print("No changes since last run. Standings are already up to date.")
    else:
        print("CHANGES DETECTED")
        print("-" * 52)
        for m in added:
            entries = result_to_entries(m)
            tags = []
            for e in entries:
                info = country_idx.get(e["country"])
                if info:
                    p = points_for(e["country"], e["stage"], e["outcome"], country_idx)
                    if p:
                        tags.append(f"{info['owner']} +{p:g} ({e['country']})")
            tag = "  ->  " + "; ".join(tags) if tags else "  (no drafted teams)"
            print(f"  NEW   {describe(m)}{tag}")
        for old_m, new_m in changed:
            print(f"  EDIT  {describe(old_m)}  =>  {describe(new_m)}")
        for m in removed:
            print(f"  GONE  {describe(m)} (no longer in feed)")
        print("-" * 52 + "\n")

    owner_totals, country_totals = compute_totals(new_completed, country_idx)

    print("STANDINGS")
    print("-" * 52)
    for rank, (owner, pts) in enumerate(sorted(owner_totals.items(), key=lambda x: -x[1]), 1):
        print(f"  {rank}. {owner:<16} {pts:>6g}")
    print("-" * 52 + "\n")

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(new_completed, f, indent=2, ensure_ascii=False)

    if regenerate_page(new_completed, country_idx):
        print(f"Page rebuilt: {PAGE_PATH}")
        print("Re-upload that file to your host to publish the update.")


if __name__ == "__main__":
    main()
