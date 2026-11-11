#!/usr/bin/env python3
"""Scripted werewolf agent for make.fun's werewolf-agent-01.

Standard library only, deterministic: every random draw comes from a
random.Random seeded by MAKEFUN_TRIAL_SEED plus the game number and a
per-game decision counter, so two runs of the same trial draw the same
values. No clock is ever read, and env requests go out strictly one at a
time.
"""

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "http://store:8080").rstrip("/")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED", "0")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_CHARS = 380

COUNTERS = {}


def post(path, body):
    """POST one JSON object to the env; return (status, decoded answer)."""
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        ENV_URL + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with OPENER.open(req, timeout=120) as res:
            raw = res.read()
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        try:
            raw = err.read()
            payload = json.loads(raw) if raw else {}
        except (ValueError, OSError):
            payload = {}
        return err.code, payload
    except (urllib.error.URLError, OSError):
        return 0, {}


def rng_for(game):
    """A fresh Random for one decision in one game, derived from the seed."""
    n = COUNTERS.get(game, 0)
    COUNTERS[game] = n + 1
    digest = hashlib.sha256(
        (TRIAL_SEED + "|" + str(game) + "|" + str(n)).encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(digest, "big"))


# ---------------------------------------------------------------- view helpers


def me(view):
    return view.get("you") or {}


def my_seat(view):
    return me(view).get("seat")


def my_role(view):
    return me(view).get("role") or "villager"


def partner_seat(view):
    p = me(view).get("partner") or {}
    return p.get("seat")


def players(view):
    return view.get("players") or []


def name_of(view, seat):
    for p in players(view):
        if p.get("seat") == seat:
            return p.get("name") or ("seat %s" % seat)
    return "seat %s" % seat


def alive_seats(view):
    return [p["seat"] for p in players(view) if p.get("alive")]


def living_others(view, exclude_partner=False):
    seat = my_seat(view)
    skip = {seat}
    if exclude_partner:
        skip.add(partner_seat(view))
    return [s for s in alive_seats(view) if s not in skip]


def living_nonwolves(view):
    """Living seats that are not me and not my partner (for a wolf's kill)."""
    return living_others(view, exclude_partner=True)


def log_of(view):
    return view.get("log") or []


def speeches(view):
    return [e for e in log_of(view) if e.get("kind") == "speech" and e.get("text")]


def votes_cast(view):
    return [e for e in log_of(view) if e.get("kind") == "vote"]


def accused_counts(view):
    """Seat -> how often it was named: by a vote, or by name inside a speech."""
    counts = {}
    seats = alive_seats(view) + [p["seat"] for p in players(view)]
    for entry in votes_cast(view):
        t = entry.get("target")
        if t is not None:
            counts[t] = counts.get(t, 0) + 1
    by_name = {}
    for p in players(view):
        if p.get("name"):
            by_name[p["name"].lower()] = p["seat"]
    for entry in speeches(view):
        text = (entry.get("text") or "").lower()
        speaker = entry.get("seat")
        for name, seat in by_name.items():
            if seat == speaker:
                continue
            if name in text:
                counts[seat] = counts.get(seat, 0) + 1
    return counts


def most_accused(view, pool):
    counts = accused_counts(view)
    ranked = sorted(pool, key=lambda s: (-counts.get(s, 0), s))
    return [s for s in ranked if counts.get(s, 0) > 0]


def accusers_of(view, seat):
    """Living seats that named `seat`, by vote or by name, in the log."""
    out = []
    names = {p["seat"]: (p.get("name") or "").lower() for p in players(view)}
    for entry in votes_cast(view):
        if entry.get("target") == seat:
            s = entry.get("seat")
            if s is not None and s != seat:
                out.append(s)
    for entry in speeches(view):
        s = entry.get("seat")
        nm = names.get(seat, "")
        if s is not None and s != seat and nm and nm in (entry.get("text") or "").lower():
            out.append(s)
    return out


def claimed_seer(view):
    """Living seats that claimed the seer role in a speech (self excluded)."""
    seat = my_seat(view)
    out = []
    for entry in speeches(view):
        s = entry.get("seat")
        if s is None or s == seat or s in out:
            continue
        text = (entry.get("text") or "").lower()
        if "seer" in text or "inspector" in text:
            out.append(s)
    return [s for s in out if s in alive_seats(view)]


def wolf_findings(view):
    """Seats my own inspections marked as werewolves."""
    out = []
    for r in view.get("seerResults") or []:
        if r.get("werewolf") and r.get("seat") is not None and r["seat"] not in out:
            out.append(r["seat"])
    return out


def inspected(view):
    return {r.get("seat") for r in (view.get("seerResults") or [])}


def last_protected(view):
    p = view.get("lastProtect") or {}
    return p.get("seat")


def partner_vote_tonight(view):
    """The target my partner already named tonight, if it is a legal kill."""
    night = view.get("day")
    partner = partner_seat(view)
    legal = set(living_nonwolves(view))
    for v in view.get("wolfVotes") or []:
        if v.get("night") == night and v.get("seat") == partner:
            t = v.get("target")
            if t in legal:
                return t
    return None


def pick(options, wanted):
    """First wanted seat that is an allowed option."""
    allowed = set(options)
    for s in wanted:
        if s in allowed:
            return s
    return None


def clip(text):
    """One line, well under the 500-character statement limit."""
    text = " ".join((text or "").split())
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 3].rstrip() + "..."
    return text


def reason_against(view, seat):
    """A concrete reason to name `seat`, drawn from the public log."""
    if my_seat(view) in accusers_of(view, seat):
        return "came at me without answering my question"
    for v in votes_cast(view):
        if v.get("target") == seat:
            return "voted %s on day %s and never explained it" % (
                name_of(view, v.get("seat")),
                v.get("day"),
            )
    if my_seat(view) in alive_seats(view):
        if seat in accusers_of(view, my_seat(view)):
            return "leaned on me with nothing solid"
    return "has been coasting while others carry the table"


def villager_suspect(view, rng):
    pool = living_others(view)
    if not pool:
        return None
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    mine = [s for s in pool if s in accusers_of(view, my_seat(view))]
    if mine:
        return mine[0]
    return rng.choice(pool)


def wolf_suspect(view, rng):
    """A village seat to accuse: one that accused me or my partner first."""
    partner = partner_seat(view)
    pool = living_others(view, exclude_partner=True)
    if not pool:
        return None
    enemies = []
    for s in pool:
        if s in accusers_of(view, my_seat(view)):
            enemies.append(s)
        elif partner is not None and s in accusers_of(view, partner):
            enemies.append(s)
    if enemies:
        return enemies[0]
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    return rng.choice(pool)


def speak_text(view, rng):
    seat = my_seat(view)
    role = my_role(view)
    if role == "seer":
        finds = [s for s in wolf_findings(view) if s in alive_seats(view)]
        if finds:
            names = ", ".join(name_of(view, s) for s in finds[:2])
            return clip(
                "I am the seer. My night inspections came back werewolf on %s. "
                "Vote them out and I will keep feeding you results until the wolves come for me." % names
            )
        if view.get("day", 1) >= 2:
            return clip(
                "I am the seer and my inspections so far are clean. Nothing confirmed, so keep the quiet seats talking "
                "and let me take one more night."
            )
        return clip("Seat %s here, reading the table. %s is my first suspect: %s." % (
            seat, name_of(view, villager_suspect(view, rng) or seat),
            reason_against(view, villager_suspect(view, rng) or seat)))
    if role == "werewolf":
        target = wolf_suspect(view, rng)
        partner = me(view).get("partner") or {}
        if target is None:
            return clip("Villager here. Nothing jumps out yet, so I am listening before I commit.")
        tail = ""
        if partner.get("seat") and partner.get("alive"):
            tail = " %s is with me on this." % name_of(view, partner["seat"])
        return clip(
            "Villager here. %s is my suspect: %s.%s" % (name_of(view, target), reason_against(view, target), tail)
        )
    target = villager_suspect(view, rng)
    if target is None:
        return clip("Nothing solid on the board yet. Everyone talk, and I will commit once someone slips.")
    return clip("%s is my top suspect: %s. I will vote there." % (name_of(view, target), reason_against(view, target)))


def vote_target(view, options, rng):
    known = [s for s in wolf_findings(view) if s in options]
    if known:
        return known[0]
    pool = [s for s in options if s != partner_seat(view)]
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    if not pool:
        return None
    return rng.choice(pool)


def kill_target(view, options, rng):
    """Prefer a claimed seer, then a seat that accused a wolf, else at random."""
    allowed = set(options)
    mate = partner_vote_tonight(view)
    if mate is not None:
        return mate
    claimant = pick(options, claimed_seer(view))
    if claimant is not None:
        return claimant
    wolves = set(wolf_findings(view))
    if my_role(view) == "werewolf":
        wolves.add(my_seat(view))
        if partner_seat(view) is not None:
            wolves.add(partner_seat(view))
    for v in votes_cast(view):
        if v.get("target") in wolves and v.get("seat") in allowed and v.get("seat") not in wolves:
            return v["seat"]
    pool = [s for s in options if s not in wolves]
    if not pool:
        pool = list(options)
    if not pool:
        return None
    return rng.choice(pool)


def protect_target(view, options, rng):
    """A claimed seer if one is alive, else the loudest talker; never me, never last night's seat."""
    seat = my_seat(view)
    banned = {seat, last_protected(view)}
    pool = [s for s in options if s not in banned]
    if not pool:
        pool = [s for s in options if s != seat] or list(options)
    if not pool:
        return None
    claimant = pick(pool, claimed_seer(view))
    if claimant is not None:
        return claimant
    noise = {}
    for e in speeches(view):
        s = e.get("seat")
        if s is not None:
            noise[s] = noise.get(s, 0) + 1
    ranked = sorted(pool, key=lambda s: (-noise.get(s, 0), s))
    if ranked and noise.get(ranked[0], 0):
        return ranked[0]
    return rng.choice(pool)


def inspect_target(view, options, rng):
    """A living, uninspected, non-self seat, most-accused first, else at random."""
    seat = my_seat(view)
    seen = inspected(view)
    pool = [s for s in options if s != seat and s not in seen]
    if not pool:
        pool = [s for s in options if s != seat] or list(options)
    if not pool:
        return None
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    return rng.choice(pool)


def said_enough(view):
    """True when I already spoke my fill this day."""
    seat = my_seat(view)
    day = view.get("day")
    mine = [e for e in log_of(view) if e.get("kind") == "speech" and e.get("seat") == seat and e.get("day") == day]
    return len(mine) >= 2


def decide(item):
    """The (kind, payload) answer for one pending decision."""
    game = item.get("game")
    kind = item.get("decision")
    view = item.get("view") or {}
    rng = rng_for(game)
    options = item.get("options") or []
    if kind == "speak":
        if said_enough(view):
            return kind, {"pass": True}
        text = speak_text(view, rng)
        if not text:
            return kind, {"pass": True}
        return kind, {"text": text}
    if kind == "vote":
        return kind, {"target": vote_target(view, options, rng)}
    if kind == "kill":
        return kind, {"target": kill_target(view, options, rng)}
    if kind == "protect":
        return kind, {"target": protect_target(view, options, rng)}
    if kind == "inspect":
        return kind, {"target": inspect_target(view, options, rng)}
    return None, None


def answer(item):
    """Act on one pending item; re-observe and retry once on a 400 or 409."""
    game = item.get("game")
    for attempt in (0, 1):
        kind, payload = decide(item)
        if kind is None:
            return
        body = {"game": game, "decision": kind}
        body.update(payload)
        status, answer_json = post("/act", body)
        if 200 <= status < 300:
            return
        if status in (400, 409) and attempt == 0:
            status2, obs = post("/observe", {})
            for again in obs.get("pending") or []:
                if again.get("game") == game:
                    item = again
                    break
            continue
        log("act refused game=%s kind=%s status=%s %s" % (game, kind, status, json.dumps(answer_json)[:160]))
        return


def log(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def read_brief():
    """Read the brief; only its game count matters here."""
    path = os.environ.get("MAKEFUN_PROMPT_FILE")
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def main():
    brief = read_brief()
    log("werewolf agent up: brief %d chars, env %s" % (len(brief), ENV_URL))
    misses = 0
    games_seen = set()
    while True:
        status, obs = post("/observe", {})
        if status < 200 or status >= 300:
            misses += 1
            if misses > 60:
                log("observe keeps failing (%s), giving up" % status)
                return 1
            time.sleep(0.5)
            continue
        misses = 0
        for item in obs.get("pending") or []:
            games_seen.add(item.get("game"))
            answer(item)
        if obs.get("done"):
            log("trial done: %s games, %s finished" % (obs.get("games"), obs.get("finished")))
            return 0


if __name__ == "__main__":
    sys.exit(main())

