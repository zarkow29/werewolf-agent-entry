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
MAX_CHARS = 290

COUNTERS = {}
# Per-game memory that a view alone cannot carry: the false finding a wolf's
# counter-claim must keep, whether it already counter-claimed, and whether the
# doctor's last line named a target (so two such lines never run together).
FALSE_FINDING = {}
WOLF_CLAIMED = {}
DOCTOR_SAID = {}
MAX_CLAIM = 299


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


def revealed_roles(view):
    """Seat -> role for every seat whose role the game has revealed."""
    return {p["seat"]: p.get("role") for p in players(view) if p.get("role")}


def believed_suspects(view, pool):
    """Pool ordered by what the revealed dead roles say about each seat.

    A seat that voted for a revealed werewolf gets credit, a seat that voted for
    a revealed good seat is a suspect; ties keep the lowest seat.
    """
    revealed = revealed_roles(view)
    score = {s: 0 for s in pool}
    for e in votes_cast(view):
        s, t = e.get("seat"), e.get("target")
        if s not in score or t is None:
            continue
        role = revealed.get(t)
        if role == "werewolf":
            score[s] -= 2
        elif role:
            score[s] += 1
    return sorted(pool, key=lambda s: (score[s], s))


def claimed_doctor(view):
    """Living seats other than me that claimed the doctor in a speech."""
    seat = my_seat(view)
    alive = alive_seats(view)
    out = []
    for e in speeches(view):
        s = e.get("seat")
        if s is None or s == seat or s in out or s not in alive:
            continue
        text = named_by(e)
        if "doctor" in text or "medic" in text:
            out.append(s)
    return out


def doctor_cover(view, finds):
    """The claimed doctor that spoke of one of my findings, else any claimed doctor."""
    docs = claimed_doctor(view)
    if not docs:
        return None
    for d in docs:
        spoken = " " + " ".join(named_by(e) for e in speeches(view) if e.get("seat") == d)
        for s in finds:
            nm = (name_of(view, s) or "").lower()
            if nm and nm in spoken:
                return d
    return docs[0]


def seer_claim(view, finds):
    """The full claim: newest wolf first, then the running list, then the doctor."""
    newest = finds[-1]
    text = "seer, night %s inspected %s, %s is a werewolf, exile %s" % (
        inspect_night(view, newest),
        name_of(view, newest),
        name_of(view, newest),
        name_of(view, newest),
    )
    older = []
    for s in reversed(finds[:-1]):
        older.append("night %s: %s" % (inspect_night(view, s), name_of(view, s)))
    tail = "; earlier " + ", ".join(older) + " all wolves" if older else ""
    if len(text + tail) > MAX_CLAIM - 45:
        tail = ""
    text += tail
    doc = doctor_cover(view, finds)
    if doc is not None:
        text += "; doctor %s is covering me" % name_of(view, doc)
    return clip(text[: MAX_CLAIM - 3] if len(text) > MAX_CLAIM else text)


def pressure_text(view, rng):
    """Before any finding: name the most-accused seat and put its votes to it."""
    pool = living_others(view)
    if not pool:
        return "Seat %s here. Seer, speak up if you have anything." % my_seat(view)
    hot = most_accused(view, pool)
    seat = hot[0] if hot else rng.choice(pool)
    theirs = [e for e in votes_cast(view) if e.get("seat") == seat and e.get("target") is not None]
    if theirs:
        last = theirs[-1]
        return clip(
            "%s, you voted %s on day %s: was that a wolf read or a shrug? Say which seat is "
            "clearing you, because the table keeps naming you." % (name_of(view, seat), name_of(view, last.get("target")), last.get("day"))
        )
    return clip(
        "%s, the table keeps naming you and you have not voted once: name the seat you would "
        "exile today and the seat you would clear." % name_of(view, seat)
    )


def counter_claim(view, rng):
    """A wolf's false seer claim: one village seat and night, fixed for the whole game."""
    seat = my_seat(view)
    game = view.get("game")
    banned = {seat, partner_seat(view)}
    banned.update(wolves_set(view))
    alive = alive_seats(view)
    held = FALSE_FINDING.get(game)
    if held is not None and held[0] in alive:
        target, night = held
    else:
        pool = [s for s in alive if s not in banned]
        if not pool:
            pool = [s for s in living_others(view, exclude_partner=True)]
        if not pool:
            return None
        hot = accused_wolf_most(view, pool)
        target = hot if hot is not None else most_accused(view, pool)[0] if most_accused(view, pool) else rng.choice(pool)
        if target is None:
            target = rng.choice(pool)
        night = max(1, view.get("day", 2) - 1)
        FALSE_FINDING[game] = (target, night)
    rival = sorted(claimed_seer(view), key=lambda s: (-trust_score(view, s), s))
    tail = ""
    if rival:
        tail = "; %s is the lie" % name_of(view, rival[0])
    return clip(
        "seer, night %s inspected %s, %s is a werewolf, exile %s%s"
        % (night, name_of(view, target), name_of(view, target), name_of(view, target), tail)
    )


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


def wolves_set(view):
    """Every werewolf seat the table has: my own, my partner, my findings."""
    out = set(wolf_findings(view))
    if my_seat(view) is not None:
        out.add(my_seat(view))
    if partner_seat(view) is not None:
        out.add(partner_seat(view))
    return out


def named_by(entry):
    """Lowercased text of one log entry, empty when it has none."""
    return (entry.get("text") or "").lower()


def wolf_accused_counts(view, pool):
    """Seat -> how often it named a werewolf, by vote or inside a speech."""
    wolves = wolves_set(view)
    names = {p["seat"]: (p.get("name") or "").lower() for p in players(view)}
    counts = {}
    for entry in votes_cast(view):
        s = entry.get("seat")
        if s in pool and entry.get("target") in wolves:
            counts[s] = counts.get(s, 0) + 1
    for entry in speeches(view):
        s = entry.get("seat")
        if s not in pool:
            continue
        text = named_by(entry)
        for w in wolves:
            nm = names.get(w, "")
            if nm and nm in text:
                counts[s] = counts.get(s, 0) + 1
                break
    return counts


def accused_wolf_most(view, pool):
    """The seat that most often went at a werewolf, lowest seat on a tie."""
    if not pool:
        return None
    counts = wolf_accused_counts(view, set(pool))
    ranked = sorted(pool, key=lambda s: (-counts.get(s, 0), s))
    if ranked and counts.get(ranked[0], 0) > 0:
        return ranked[0]
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
    day = view.get("day")
    for v in votes_cast(view):
        if v.get("target") == seat:
            return "voted %s on day %s and never explained it" % (
                name_of(view, v.get("seat")),
                v.get("day"),
            )
    mine = my_seat(view)
    my_name = (name_of(view, mine) or "").lower()
    theirs = [e for e in speeches(view) if e.get("seat") == seat]
    for e in theirs:
        if my_name and my_name in named_by(e):
            return "named me on day %s and offered nothing behind it" % e.get("day")
    if not theirs:
        return "sat out the whole day %s debate without naming a seat" % day
    last = theirs[-1]
    return "spent day %s pushing a case with no vote to back it up" % last.get("day")


def claim_names(view, claimant):
    """Living seats a seat named in its own speeches."""
    nm = {}
    for p in players(view):
        if p.get("name"):
            nm[p["seat"]] = p["name"].lower()
    out = []
    for e in speeches(view):
        if e.get("seat") != claimant:
            continue
        text = named_by(e)
        for s, name in nm.items():
            if s != claimant and name and name in text and s not in out:
                out.append(s)
    return out


def trust_score(view, claimant):
    """Credit for a claim: naming a revealed werewolf counts for, a revealed good seat against."""
    revealed = {p["seat"]: p.get("role") for p in players(view) if p.get("role")}
    score = 0
    for s in claim_names(view, claimant):
        role = revealed.get(s)
        if role == "werewolf":
            score += 2
        elif role:
            score -= 1
    return score


def trusted_claim(view):
    """The claimed seer the revealed dead roles back, or None with no claim."""
    claims = claimed_seer(view)
    if not claims:
        return None
    return sorted(claims, key=lambda s: (-trust_score(view, s), s))[0]


def votes_on_good(view, pool):
    """Seats whose votes landed on a confirmed-good dead player."""
    good = {p["seat"] for p in players(view) if p.get("role") and p["role"] != "werewolf"}
    out = []
    for e in votes_cast(view):
        s = e.get("seat")
        if s in pool and e.get("target") in good and s not in out:
            out.append(s)
    return sorted(out)


def villager_suspect(view, rng):
    pool = living_others(view)
    if not pool:
        return None
    trusted = trusted_claim(view)
    if trusted is not None:
        named = [s for s in claim_names(view, trusted) if s in pool]
        if named:
            return named[-1]
    mine = [s for s in pool if s in accusers_of(view, my_seat(view))]
    if mine:
        return mine[0]
    ranked = believed_suspects(view, pool)
    if ranked:
        return ranked[0]
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    return rng.choice(pool)


def wolf_suspect(view, rng):
    """A village seat to accuse, aligned with the night pick: whoever went at a wolf."""
    pool = living_others(view, exclude_partner=True)
    if not pool:
        return None
    hot = accused_wolf_most(view, pool)
    if hot is not None:
        return hot
    spread = most_accused(view, pool)
    if spread:
        return spread[0]
    return rng.choice(pool)


def inspect_night(view, seat):
    """The night my finding on `seat` was made."""
    for r in view.get("seerResults") or []:
        if r.get("seat") == seat:
            return r.get("night")
    return None


def speak_text(view, rng):
    seat = my_seat(view)
    role = my_role(view)
    game = view.get("game")
    if role == "seer":
        finds = [s for s in wolf_findings(view) if s in alive_seats(view)] or wolf_findings(view)
        if finds:
            return seer_claim(view, finds)
        press = pressure_text(view, rng)
        if view.get("day", 1) >= 2:
            return clip("seer, nothing but village so far. " + press)
        return press
    if role == "werewolf":
        claims = claimed_seer(view)
        if claims and view.get("day", 1) >= 2:
            WOLF_CLAIMED[game] = True
        if WOLF_CLAIMED.get(game):
            told = counter_claim(view, rng)
            if told:
                return told
        target = wolf_suspect(view, rng)
        partner = me(view).get("partner") or {}
        pseat = partner.get("seat")
        shout = []
        if pseat is not None and pseat in alive_seats(view):
            shout = [s for s in accusers_of(view, pseat) if s in alive_seats(view) and s != my_seat(view)]
        if target is None:
            return clip("Villager here. Nothing jumps out yet, so I am listening before I commit.")
        if shout:
            other = shout[0]
            tail = ""
            if target != other:
                tail = " %s is my own suspect: %s." % (name_of(view, target), reason_against(view, target))
            return clip(
                "Villager here. %s still reads village to me, the vote on %s was weak.%s I am on %s."
                % (name_of(view, pseat), name_of(view, other), tail, name_of(view, other))
            )
        tail = ""
        if pseat is not None and pseat in alive_seats(view):
            tail = " %s is with me on this." % name_of(view, pseat)
        return clip(
            "Villager here. %s is my suspect: %s.%s" % (name_of(view, target), reason_against(view, target), tail)
        )
    if role == "doctor":
        doc = pick(living_others(view), claimed_seer(view)) or pick(living_others(view), claimed_doctor(view))
        spare = doc is not None and doc != last_protected(view)
        if not DOCTOR_SAID.get(game) and (spare or doc is None):
            DOCTOR_SAID[game] = True
            if doc is not None:
                return clip(
                    "I am the doctor and %s is on my sheet: the seer claim is covered tonight, "
                    "so keep the table talking and do not burn the day on it." % name_of(view, doc)
                )
            return clip(
                "I am the doctor: whoever claims seer gets my cover tonight, so speak up if you have a read."
            )
        DOCTOR_SAID[game] = False
        target = villager_suspect(view, rng)
        if target is None:
            return clip("I am the doctor and my sheet is set. Bring me a case, not a hunch.")
        return clip(
            "I am the doctor, my sheet is set. %s is my suspect: %s."
            % (name_of(view, target), reason_against(view, target))
        )
    trusted = trusted_claim(view)
    if trusted is not None:
        want = [s for s in claim_names(view, trusted) if s in living_others(view)]
        if want:
            return clip(
                "Following the seer's case on %s, the claim on %s holds: %s. I vote %s."
                % (name_of(view, trusted), name_of(view, want[-1]), reason_against(view, want[-1]), name_of(view, want[-1]))
            )
        return clip(
            "Following %s's seer claim; nothing it named is left alive, so I back the claim itself."
            % name_of(view, trusted)
        )
    target = villager_suspect(view, rng)
    if target is None:
        return clip("Nothing solid on the board yet. Everyone talk, and I will commit once someone slips.")
    return clip("%s is my top suspect: %s. I will vote there." % (name_of(view, target), reason_against(view, target)))


def vote_target(view, options, rng):
    """Never abstain while a living seat other than me is on the ballot."""
    seat = my_seat(view)
    pool = [s for s in options if s != seat] or list(options)
    if not pool:
        return None
    role = my_role(view)
    known = [s for s in wolf_findings(view) if s in pool]
    if role == "seer" and known:
        return known[-1]
    if role == "werewolf":
        mine = FALSE_FINDING.get(view.get("game"))
        if WOLF_CLAIMED.get(view.get("game")) and mine is not None and mine[0] in pool:
            return mine[0]
        pool = [s for s in pool if s != partner_seat(view)] or pool
        hot = accused_wolf_most(view, pool)
        if hot is not None:
            return hot
        return believed_suspects(view, pool)[0]
    trusted = trusted_claim(view)
    if trusted is not None:
        want = [s for s in claim_names(view, trusted) if s in pool]
        if want:
            return want[-1]
    ranked = believed_suspects(view, pool)
    if ranked:
        return ranked[0]
    hot = most_accused(view, pool)
    if hot:
        return hot[0]
    return rng.choice(pool)


def kill_target(view, options, rng):
    """Converge with the partner: mirror its pick, else the highest threat living non-wolf.

    Mirroring is what actually kills somebody: two different seats kill nobody.
    The fallback is a rule both wolves compute from the same public log, so the
    partner, voting second and seeing this pick, lands on the same seat.
    """
    if not options:
        return None
    mate = partner_vote_tonight(view)
    if mate is not None:
        return mate
    claimed = pick(options, claimed_seer(view))
    if claimed is not None:
        return claimed
    hot = accused_wolf_most(view, list(options))
    if hot is not None:
        return hot
    return min(options)


def protect_target(view, options, rng):
    """A claimed seer while it lives, else the most useful talker; never me, never last night's seat."""
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
    weight = {}
    for e in speeches(view):
        s = e.get("seat")
        if s in pool:
            weight[s] = weight.get(s, 0) + len(named_by(e))
    ranked = sorted(pool, key=lambda s: (-weight.get(s, 0), s))
    return ranked[0] if ranked else None


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
    view = dict(item.get("view") or {})
    view["game"] = game
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
    FALSE_FINDING.clear()
    WOLF_CLAIMED.clear()
    DOCTOR_SAID.clear()
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

