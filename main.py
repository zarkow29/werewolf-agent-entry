#!/usr/bin/env python3
"""Scripted werewolf agent for make.fun's werewolf-agent-01.

Standard library only, deterministic: every random draw comes from a
random.Random seeded by MAKEFUN_TRIAL_SEED plus the game number and a
decision key derived from the view, so two runs of the same trial draw the
same values and a repeated decision for the same state returns the same
action.

Determinism rules kept here, as comments and not as runtime checks:
  * nothing reads the clock -- time is used for sleeps only, and no sleep
    length, elapsed time or timestamp is ever put in a request or a statement;
    the only "now" the agent has is the view the env serves;
  * no randomness outside rng_for(): no random.random(), no uuid, no
    os.urandom, no set/dict iteration that a hash seed could reorder where it
    reaches an answer (seats are always sorted or int-keyed);
  * the decision key is (game, day, phase, kind, log length), all of which
    come from the env, never from the order the games happened to be answered
    in, so the live pass and the replay answer alike;
  * env requests go out strictly one at a time, in the order they were made.
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
# A statement is folded to one line by the env and refused past its limit; we
# stay far below the env's 500 so a long name list or a clipped tail can never
# be the reason an act is refused. HARD_CLIP counts the ellipsis.
MAX_CHARS = 280
HARD_CLIP = 280
OBSERVE_TIMEOUT = 25
ACT_TIMEOUT = 25
MAX_STATEMENT_TRIES = 3

# Per-game memory that a view alone cannot carry: the false finding a wolf's
# counter-claim must keep, whether it already counter-claimed, and whether the
# doctor's last line named a target (so two such lines never run together).
FALSE_FINDING = {}
WOLF_CLAIMED = {}
DOCTOR_SAID = {}
# (game, day) -> consecutive passes, so a debate turn cycle that has already
# passed twice stops burning turns on statements.
PASSES = {}


def post(path, body, timeout=ACT_TIMEOUT):
    """POST one JSON object to the env; return (status, decoded answer).

    A transport failure (socket timeout included) answers status 0 with an
    empty payload, so a caller can treat it as "nothing new" instead of dying.
    """
    try:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            ENV_URL + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with OPENER.open(req, timeout=timeout) as res:
            raw = res.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            return res.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as err:
        try:
            raw = err.read()
            payload = json.loads(raw) if raw else {}
        except (ValueError, OSError):
            payload = {}
        return err.code, payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, OSError, TimeoutError):
        # URLError wraps socket.timeout; OSError covers a bare one.
        return 0, {}


def decision_key(view, kind):
    """A key that is the same for every repeat of one pending decision.

    All parts come from the env, never from a counter, so answering a game
    twice in a row, or after a 400 plus a re-observe, draws the same values and
    sends the same action again.
    """
    return "|".join(
        (
            str(view.get("game")),
            str(view.get("day")),
            str(view.get("phase")),
            str(kind),
            str(len(log_of(view))),
        )
    )


def rng_for(view, kind):
    """A Random for one decision, derived from the seed and the decision key."""
    digest = hashlib.sha256(
        (TRIAL_SEED + "|" + decision_key(view, kind)).encode("utf-8")
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


def cleared_seat(view):
    """Newest living seat my own inspections cleared, else the newest cleared one.

    A cleared seat is what an early claim has to sell: the claim is credible
    because the inspection is real, not because a wolf finding exists yet.
    """
    rows = cleared_seats(view)
    return rows[0] if rows else None


def cleared_seats(view):
    """Cleared seats, newest first, living ones preferred, so a claim can name two."""
    rows = [
        r
        for r in (view.get("seerResults") or [])
        if not r.get("werewolf") and r.get("seat") is not None
    ]
    if not rows:
        return []
    alive = set(alive_seats(view))
    live = [r for r in rows if r["seat"] in alive]
    return [r["seat"] for r in reversed(live or rows)]


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


def claim_like(text):
    """True when a folded line reads like a seer claim, not just a mention of one."""
    if "seer" not in text and "inspector" not in text:
        return False
    return "night" in text or "inspect" in text or "here" in text


def burned_claim(view):
    """True when a seat that claimed the seer was exiled and revealed a werewolf.

    Only a claim counts, not a mere mention of the role: a wolf that said "the
    seer should speak" must not burn every later claimant's cover.
    """
    revealed = revealed_roles(view)
    seat = my_seat(view)
    for e in speeches(view):
        s = e.get("seat")
        if s is None or s == seat:
            continue
        if revealed.get(s) != "werewolf":
            continue
        if claim_like(named_by(e)):
            return True
    return False


def believable_claim(view, pool):
    """Claimed seers in `pool` that nothing revealed contradicts and trust backs.

    A claim the table already burned, or one whose trust has gone negative, is
    not worth a night's cover; the caller falls through to the weight ranking.
    """
    revealed = revealed_roles(view)
    seen_seer = [s for s, r in revealed.items() if r == "seer"]
    out = []
    for s in claim_order(view, [c for c in claimed_seer(view) if c in pool]):
        role = revealed.get(s)
        if role and role != "seer":
            continue
        if seen_seer and s not in seen_seer:
            continue
        if trust_score(view, s) < 0:
            continue
        out.append(s)
    return out


def cover_claimant(view, pool=None):
    """The claimed seer the doctor may cover tonight, None when the claim is burned.

    A burned claim (its claimant exiled and revealed a werewolf) means the next
    claimant is the wolf's echo, so no claimant gets cover at all and the
    caller falls through to the weight ranking.
    """
    if burned_claim(view):
        return None
    pool = list(pool) if pool is not None else living_others(view)
    return pick(pool, believable_claim(view, pool))


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
    """The full claim: newest wolf first, seat spelled out, then the rest of the sheet.

    The table has to be told where to put its vote, so the wagon is a seat
    number and not a name it might misspell. Repeating this every day after the
    first finding is the point: silence costs more than a repeated case.
    """
    newest = finds[-1]
    text = "seer, night %s inspected %s: werewolf. Take seat %s, exile %s" % (
        inspect_night(view, newest),
        name_of(view, newest),
        newest,
        name_of(view, newest),
    )
    older = [
        "night %s: %s (seat %s)" % (inspect_night(view, s), name_of(view, s), s)
        for s in reversed(finds[:-1])
    ]
    tail = "; earlier " + ", ".join(older) + " all wolves" if older else ""
    if len(text + tail) > MAX_CHARS - 60:
        tail = ""
    text += tail + "."
    doc = doctor_cover(view, finds)
    if doc is not None:
        text += "; doctor %s is covering me" % name_of(view, doc)
    return clip(text, MAX_CHARS - 1)


def early_claim(view, seat=None, rng=None):
    """Claim with no wolf yet: the cleared seats carry it, plus a named suspect.

    The claim is only worth making if it says something checkable, so each
    cleared seat comes with its own night, the suspect the table should push is
    named and spelled as a seat number, and the doctor is asked for cover.
    """
    if rng is None:
        rng = random.Random(0)
    cleared = cleared_seats(view)[:2]
    pool = [s for s in living_others(view) if s not in cleared and s != seat]
    suspect = villager_suspect(view, rng) if pool else None
    if suspect in cleared:
        # A cleared seat cannot be the wagon it is also asked to carry.
        suspect = None
    if suspect is None and pool:
        hot = most_accused(view, pool)
        suspect = hot[0] if hot else pool[0]
    if cleared:
        named = ", ".join(
            "%s (seat %s, night %s)" % (name_of(view, s), s, inspect_night(view, s))
            for s in cleared
        )
        text = "seer: my inspections cleared %s, no wolf yet." % named
    else:
        text = "seer here, and no wolf on my sheet yet."
    if suspect is not None:
        text += " Suspect is %s: take seat %s." % (name_of(view, suspect), suspect)
    text += " Doctor cover me."
    return clip(text, MAX_CHARS - 1)


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
    rival = claim_order(view, claimed_seer(view))
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


def clip(text, limit=HARD_CLIP):
    """One line, hard-clipped at `limit` code points with the ellipsis inside the count."""
    text = " ".join((text or "").split())
    if limit <= 3:
        return text[:limit]
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
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


def claim_started(view, seat):
    """(day, log index) of a seat's first speech naming the seer role.

    A wolf only counter-claims once a real claim is on the table, so the earlier
    claimant is the likelier true seer. A claim-like line beats a mere mention,
    and a seat that never spoke of the role at all sorts last.
    """
    mention = (999, 999)
    for i, e in enumerate(log_of(view)):
        if e.get("kind") != "speech" or e.get("seat") != seat:
            continue
        text = named_by(e)
        if "seer" not in text and "inspector" not in text:
            continue
        day = e.get("day")
        when = (day if isinstance(day, int) else 0, i)
        if claim_like(text):
            return when
        if mention == (999, 999):
            mention = when
    return mention


def claim_order(view, seats):
    """Claims by trust, then by who claimed first, then by the lowest seat."""
    return sorted(seats, key=lambda s: (-trust_score(view, s), claim_started(view, s), s))


def trusted_claim(view):
    """The claimed seer the revealed dead roles back, or None with no claim.

    Trust first, but a tie goes to the earlier claim: the wolf's counter-claim
    necessarily lands after the true seer's, so the tie is not really a tie.
    """
    claims = claimed_seer(view)
    if not claims:
        return None
    return claim_order(view, claims)[0]


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
        day = view.get("day", 1)
        if finds:
            # A finding is the whole reason to claim: speak on the day it lands
            # and repeat the full sheet every day after, dead wolves included.
            return seer_claim(view, finds)
        if day >= 3:
            # No wolf yet: the claim only starts paying once there are two
            # inspections behind it, and it must name the cleared seat.
            return early_claim(view, cleared_seat(view), rng)
        press = pressure_text(view, rng)
        return press
    if role == "werewolf":
        claims = claimed_seer(view)
        if claims:
            # A real claim is enough, on any day: the counter-claim only works
            # while the table is still deciding whom to believe.
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
        doc = cover_claimant(view) or pick(living_others(view), claimed_doctor(view))
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


def contradicted_claimants(view, pool):
    """Claimed seers in `pool` whose claim the record contradicts.

    Either a reveal already named them something else, or their own named seats
    did more harm than good (negative trust).
    """
    revealed = revealed_roles(view)
    claims = set(claimed_seer(view))
    out = []
    for s in pool:
        if s not in claims:
            continue
        role = revealed.get(s)
        if role and role != "seer":
            out.append(s)
        elif trust_score(view, s) < 0:
            out.append(s)
    return out


def weaker_claimant(view, pool):
    """The later of two live claimants when its claim is the weaker one, else None.

    A counter-claim is the wolf's move and it always lands after the real claim,
    so a second claimant that the claim order already ranks last is the seat to
    answer with a vote.
    """
    claims = [s for s in claimed_seer(view) if s in pool]
    if len(claims) < 2:
        return None
    order = claim_order(view, claims)
    trusted = trusted_claim(view)
    if trusted is None or order[-1] == trusted:
        return None
    return order[-1]


def vote_evidence(view, pool):
    """Pool ordered by evidence against it: vote record first, then a contradicted claim.

    The vote record is public and cheap to check, and it is what the table
    already agreed on; a claim the record contradicts, or a counter-claim the
    order ranks last, is the next best thing to put a vote on. Ties keep the
    lowest seat so two identical views vote alike.
    """
    counts = {}
    for e in votes_cast(view):
        t = e.get("target")
        if t in pool:
            counts[t] = counts.get(t, 0) + 1
    bad = set(contradicted_claimants(view, pool))
    weak = weaker_claimant(view, pool)
    if weak is not None:
        bad.add(weak)
    return sorted(pool, key=lambda s: (-counts.get(s, 0), 0 if s in bad else 1, s))


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
    ordered = vote_evidence(view, pool)
    bad = set(contradicted_claimants(view, pool))
    weak = weaker_claimant(view, pool)
    if weak is not None:
        bad.add(weak)
    trusted = trusted_claim(view)
    # Voting a claim you already believe wastes the ballot, so the trusted
    # claimant is skipped while the record is clean on it.
    pickable = [s for s in ordered if s != trusted or s in bad]
    if pickable:
        return pickable[0]
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
    """Converge with the partner: mirror its pick, else the biggest threat, in order.

    Mirroring is what actually kills somebody: two different seats kill nobody.
    The fallback is a rule both wolves compute from the same public log, so the
    partner, voting second and seeing this pick, lands on the same seat. After
    the partner's pick the order is: a claimed seer, a claimed doctor, the seat
    that went at a wolf hardest, then the lowest seat as the last resort.
    """
    if not options:
        return None
    mate = partner_vote_tonight(view)
    if mate is not None:
        return mate
    claimed = pick(options, claim_order(view, claimed_seer(view)))
    if claimed is not None:
        return claimed
    doctor = pick(options, claim_order(view, claimed_doctor(view)))
    if doctor is not None:
        return doctor
    hot = accused_wolf_most(view, list(options))
    if hot is not None:
        return hot
    return min(options)


def protect_target(view, options, rng):
    """Cover a believable seer claim, else the most useful talker; never me, never last night's seat.

    A claimed seer is only worth a night when nothing revealed contradicts it
    (no dead good role it is not, no trust gone negative). Once a claimant was
    exiled and revealed a werewolf, the next claim is the wolf's echo, so cover
    stops and the talk-weight ranking decides.
    """
    seat = my_seat(view)
    banned = {seat, last_protected(view)}
    pool = [s for s in options if s not in banned]
    if not pool:
        pool = [s for s in options if s != seat] or list(options)
    if not pool:
        return None
    claimant = cover_claimant(view, pool)
    if claimant is not None:
        return claimant
    if claimed_seer(view):
        # Every claim on the table was refused (burned, contradicted or trust
        # gone negative), so no claimant is worth a night: the weight ranking
        # decides among the seats that never claimed.
        rest = [s for s in pool if s not in claimed_seer(view)]
        if rest:
            pool = rest
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


def safe_payload(kind, options):
    """The safest legal action when a decision blew up: pass, else the first option."""
    if kind == "speak":
        return {"pass": True}
    if options:
        return {"target": options[0]}
    return {"target": None}


def speak_decision(view, item):
    """A statement, or a pass once this debate cycle has passed twice already."""
    key = (view.get("game"), view.get("day"))
    if PASSES.get(key, 0) >= 2:
        return {"pass": True}
    if said_enough(view):
        PASSES[key] = PASSES.get(key, 0) + 1
        return {"pass": True}
    try:
        text = speak_text(view, rng_for(view, "speak"))
    except Exception as exc:  # a broken speech is a pass, never a crash
        log("speech failed game=%s: %s: %s" % (view.get("game"), type(exc).__name__, exc))
        text = ""
    limit = item.get("max_chars")
    if not isinstance(limit, int) or limit <= 3:
        limit = HARD_CLIP
    text = clip(text, min(HARD_CLIP, limit))
    if not text:
        PASSES[key] = PASSES.get(key, 0) + 1
        return {"pass": True}
    PASSES[key] = 0
    return {"text": text}


def decide(item):
    """The (kind, payload) answer for one pending decision, safe against any view."""
    game = item.get("game")
    kind = item.get("decision")
    options = [o for o in (item.get("options") or []) if isinstance(o, int)]
    if not options and item.get("options"):
        options = list(item.get("options") or [])
    view = dict(item.get("view") or {})
    view["game"] = game
    try:
        if kind == "speak":
            return kind, speak_decision(view, item)
        rng = rng_for(view, kind)
        if kind == "vote":
            return kind, {"target": vote_target(view, options, rng)}
        if kind == "kill":
            return kind, {"target": kill_target(view, options, rng)}
        if kind == "protect":
            return kind, {"target": protect_target(view, options, rng)}
        if kind == "inspect":
            return kind, {"target": inspect_target(view, options, rng)}
        return None, None
    except Exception as exc:
        # One malformed view or odd option list must not kill the loop: answer
        # the safest legal action and let the game carry on.
        log("decide failed game=%s kind=%s: %s: %s" % (game, kind, type(exc).__name__, exc))
        return (kind, safe_payload(kind, options)) if kind else (None, None)


def refresh(game, item):
    """Re-observe after a refusal and return that game's fresh item, else the old one."""
    status, obs = post("/observe", {}, timeout=OBSERVE_TIMEOUT)
    if 200 <= status < 300:
        for again in obs.get("pending") or []:
            if isinstance(again, dict) and again.get("game") == game:
                return again
    return item


def answer(item):
    """Act on one pending item; on a 400 or 409 re-observe and retry that game once."""
    game = item.get("game")
    for attempt in (1, 2):
        if attempt == 2:
            # The retry only helps with a fresh view; when the re-observe gave
            # the same item back, answer the safest legal action instead.
            kind, payload = decide_fallback(item)
        else:
            kind, payload = decide(item)
        if kind is None:
            return
        body = {"game": game, "decision": kind}
        body.update(payload)
        try:
            status, answer_json = post("/act", body)
        except Exception as exc:  # post already guards, this is the last net
            log("act failed game=%s kind=%s: %s" % (game, kind, exc))
            return
        if 200 <= status < 300:
            return
        if status in (400, 409) and attempt == 1:
            fresh = refresh(game, item)
            if fresh is not item and isinstance(fresh, dict) and fresh is not None:
                item = fresh
                # retry with the new item through decide(), not the fallback
                kind, payload = decide(item)
                if kind is None:
                    return
                body = {"game": game, "decision": kind}
                body.update(payload)
                try:
                    status, answer_json = post("/act", body)
                except Exception as exc:
                    log("act failed game=%s kind=%s: %s" % (game, kind, exc))
                    return
                if 200 <= status < 300:
                    return
            kind, payload = decide_fallback(item)
            if kind is None:
                return
            body = {"game": game, "decision": kind}
            body.update(payload)
            try:
                status, answer_json = post("/act", body)
            except Exception as exc:
                log("act failed game=%s kind=%s: %s" % (game, kind, exc))
                return
            if 200 <= status < 300:
                return
            log("act refused twice game=%s status=%s %s" % (game, status, json.dumps(answer_json)[:160]))
            return
        log("act refused game=%s kind=%s status=%s %s" % (game, kind, status, json.dumps(answer_json)[:160]))
        return


def decide_fallback(item):
    """The safest legal action for one pending item, never raising."""
    kind = item.get("decision")
    options = [o for o in (item.get("options") or []) if isinstance(o, int)] or list(item.get("options") or [])
    return (kind, safe_payload(kind, options)) if kind else (None, None)


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
    PASSES.clear()
    brief = read_brief()
    log("werewolf agent up: brief %d chars, env %s" % (len(brief), ENV_URL))
    misses = 0
    stalls = 0
    games_seen = set()
    streak_game, streak = None, 0
    while True:
        # The observe carries the long poll, so 25 s here is a wait, not a spin.
        status, obs = post("/observe", {}, timeout=OBSERVE_TIMEOUT)
        if status == 0:
            # A socket timeout means "nothing pending yet": the request already
            # held open on its own, so go straight round again without sleeping.
            stalls += 1
            if stalls > 400:
                log("observe keeps timing out, giving up")
                return 1
            continue
        if status < 200 or status >= 300:
            misses += 1
            if misses > 60:
                log("observe keeps failing (%s), giving up" % status)
                return 1
            time.sleep(0.25)
            continue
        misses = 0
        stalls = 0
        pending = [p for p in (obs.get("pending") or []) if isinstance(p, dict)]
        acted = 0
        for item in pending:
            game = item.get("game")
            streak = streak + 1 if game == streak_game else 1
            streak_game = game
            if streak > 3:
                # This game already burned three turns in a row; it gets its
                # turn again once another game has had one.
                continue
            games_seen.add(game)
            try:
                answer(item)
            except Exception as exc:  # the loop outlives any one bad answer
                log("answer died game=%s: %s: %s" % (game, type(exc).__name__, exc))
            acted += 1
        if obs.get("done"):
            log("trial done: %s games, %s finished" % (obs.get("games"), obs.get("finished")))
            return 0
        if not acted:
            streak_game, streak = None, 0
            time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())

