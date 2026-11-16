#!/usr/bin/env python3
"""Builds StreamCal's snow sports file (events.json).

Schedules: FIS calendar feeds (World Cup and World Championships, one feed per sport) and
the Freeride World Tour's event list. Where to watch: a small table of rules per region,
below (WATCH_SETS / watch_key). No keys, no accounts, standard library only.

usage: build_snow.py <output folder> [--gaps-ok]        writes <folder>/events.json

An outage never takes events away from the app:
- When <folder>/events.json is already there (the hosted copy, rebuilt by a job nobody
  watches), a source that can't be read, or that comes back empty, keeps the events it had
  in that file. A new file with fewer than half the old one's events isn't written.
- When there's no file to fall back on (the copy built into the app), nothing is written
  unless every source could be read. --gaps-ok writes it anyway, for a source that is
  known to be gone.

exit: 0 = written; 2 = written, but a source had a problem (see the PROBLEM lines; with an
old file it kept its old events); 1 = nothing written.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
FIS_FEED = "https://data.fis-ski.com/services/public/icalendar-feed-fis-events.html?seasoncode={season}&sectorcode={sector}&categorycode={category}"
FWT_EVENTS = "https://www.freerideworldtour.com/events/season/{season}/"

# FIS sector code -> the sport key the app filters by.
SECTORS = {"AL": "alpine", "CC": "crosscountry", "JP": "skijumping", "NK": "nordiccombined",
           "FS": "freestyle", "SB": "snowboard"}
CATEGORIES = ["WC", "WSC"]

SPORT_NAMES = {
    "alpine": "Alpine Skiing", "crosscountry": "Cross-Country", "skijumping": "Ski Jumping",
    "nordiccombined": "Nordic Combined", "freestyle": "Freestyle", "freeski": "Freeski",
    "snowboard": "Snowboard", "freeride": "Freeride",
}
SPORT_ORDER = ["alpine", "freestyle", "freeski", "snowboard", "freeride",
               "crosscountry", "skijumping", "nordiccombined"]

# ---- Where to watch (2026/27). Checked Nov 10, 2026 against U.S. Ski & Snowboard's and FIS's
# announcements. Recheck each season (and when a rights deal changes).
YOUTUBE = "https://www.youtube.com/@skiandsnowboardworldcup/streams"
PEACOCK = {"name": "Peacock", "detail": "Live and on demand", "url": "https://www.peacocktv.com/"}
YT_LIVE = {"name": "YouTube: Ski & Snowboard World Cup", "detail": "Live, free", "url": YOUTUBE}
YT_REPLAY = {"name": "YouTube: Ski & Snowboard World Cup", "detail": "Free replay about 24 hours later", "url": YOUTUBE}
FAST = {"name": "Ski & Snowboard World Cup channel",
        "detail": "Free live TV channel on Roku, Prime Video, Fire TV, Google TV, Plex and Xumo Play", "url": None}
CBC = {"name": "CBC Gem", "detail": "Live and on demand", "url": "https://gem.cbc.ca/"}
ELSEWHERE = {"name": "Your local broadcaster or FIS TV", "detail": "Rights differ by country",
             "url": "https://watch.fis-ski.com/"}
FWT_SITE = {"name": "Freeride World Tour", "detail": "Live on the tour's site", "url": "https://www.freerideworldtour.com/"}

WATCH_SETS = {
    # Alpine: every World Cup and World Championship race, whatever the host country.
    "alpine": {"US": [PEACOCK, YT_REPLAY], "CA": [CBC, FAST], "*": [ELSEWHERE]},
    # Any sport, held in the United States.
    "us-hosted": {"US": [PEACOCK, YT_REPLAY], "CA": [CBC, FAST], "*": [ELSEWHERE]},
    # Every other sport, held outside the United States.
    "world": {"US": [YT_LIVE, FAST], "CA": [CBC, FAST], "*": [ELSEWHERE]},
    "freeride": {"US": [YT_LIVE, FAST, FWT_SITE], "CA": [FAST, FWT_SITE], "*": [FWT_SITE]},
    "freeride-us": {"US": [PEACOCK, YT_REPLAY, FWT_SITE], "CA": [FAST, FWT_SITE], "*": [FWT_SITE]},
}
WATCH_NOTES = {
    "alpine": {"US": "Big races are also on NBC or NBCSN."},
    "us-hosted": {"US": "Some events are also on NBC or NBCSN."},
}


def watch_key(sport, country):
    if sport == "freeride":
        return "freeride-us" if country == "USA" else "freeride"
    if sport == "alpine":
        return "alpine"
    return "us-hosted" if country == "USA" else "world"


def fetch(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - retried, then reported
            last = e
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{url}: {last}")


def unescape(s):
    return s.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(text):
    """Yields each VEVENT as {NAME: (params, value)} (lines unfolded)."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    for block in text.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        event = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            event.setdefault(name, (params, value))
        yield event


GENDERS = {"women": "W", "men": "M", "mixed": "X", "all": "X"}
ISO3 = {"US": "USA", "AT": "AUT", "CH": "SUI", "FR": "FRA", "GE": "GEO", "CA": "CAN", "ES": "ESP",
        "AD": "AND", "IT": "ITA", "JP": "JPN", "NZ": "NZL", "NO": "NOR", "SE": "SWE", "DE": "GER"}


def fis_events(season):
    """Each feed's events under its name ("AL/WC", "FS/WSC", ...); a feed that couldn't be
    read has no entry and a line in the problems."""
    sources, problems = {}, []
    for sector, sport in SECTORS.items():
        for category in CATEGORIES:
            url = FIS_FEED.format(season=season, sector=sector, category=category)
            try:
                text = fetch(url)
            except RuntimeError as e:
                problems.append(str(e))
                continue
            if "BEGIN:VCALENDAR" not in text:
                problems.append(f"{sector}/{category}: not a calendar")
                continue
            events = sources.setdefault(f"{sector}/{category}", [])
            for ev in parse_ics(text):
                uid = ev.get("UID", ("", ""))[1].strip()
                params, start = ev.get("DTSTART", ("", ""))
                start = start.strip()
                summary = unescape(ev.get("SUMMARY", ("", ""))[1]).strip()
                desc = unescape(ev.get("DESCRIPTION", ("", ""))[1])
                cats = ev.get("CATEGORIES", ("", ""))[1].strip()
                if not uid or not start:
                    continue
                # "Gender: Women\Event: Slalom" (FIS's own missing line break) or on separate lines.
                gender = re.search(r"Gender:\s*([A-Za-z]+)", desc)
                title = re.search(r"Event:\s*([^\n]+)", desc)
                place = unescape(ev.get("LOCATION", ("", ""))[1]).strip()
                country = re.search(r"\(([A-Z]{3})\)\s+-", summary)
                name = (title.group(1).strip() if title else summary.split(" - ", 1)[-1]).strip()
                kind = cats.rsplit("-", 1)[-1] if cats else category     # WC, SWC, WSC, QUA, PR...
                this_sport = sport
                if sport == "freestyle" and re.search(r"freeski|big air|slopestyle|halfpipe", name, re.I):
                    this_sport = "freeski"
                item = {
                    "id": uid,
                    "sport": this_sport,
                    "tour": "WSC" if category == "WSC" else "WC",
                    "title": re.sub(r"^Freeski\s+", "", name) if this_sport == "freeski" else name,
                    "place": place,
                    "country": country.group(1) if country else "",
                    "gender": GENDERS.get(gender.group(1).lower(), "") if gender else "",
                }
                if "VALUE=DATE" in params or "T" not in start:
                    item["day"] = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
                else:
                    try:
                        stamp = datetime.strptime(start.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    item["start"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                    item["day"] = stamp.strftime("%Y-%m-%d")
                # Qualifications, provisional rounds and training runs (rarely shown).
                if re.search(r"QUA$|^PR$|^TRA", kind) or re.search(r"qualif|training|provisional", name, re.I):
                    item["qual"] = True
                if re.search(r"cancel", summary, re.I):
                    item["status"] = "cancelled"
                results = re.search(r"https://www\.fis-ski\.com/DB/general/results\.html\?[^\s\\]+", desc)
                if results:
                    item["url"] = results.group(0)
                item["watch"] = watch_key(this_sport, item["country"])
                events.append(item)
    return sources, problems


def fwt_events(season):
    text = fetch(FWT_EVENTS.format(season=season))
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not m:
        raise RuntimeError("Freeride World Tour: page layout changed")
    data = json.loads(m.group(1))
    found = []

    def walk(o):
        if isinstance(o, dict):
            if {"start", "end", "title", "link"} <= o.keys() and o.get("season") == season:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    events, seen = [], set()
    for e in found:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        country = ISO3.get(e.get("country") or "", e.get("country") or "")
        title = re.sub(r"^\d{4}\s+", "", e["title"]).strip()
        item = {
            "id": f"FWT{e['id']}",
            "sport": "freeride",
            "tour": "FWT",
            "title": title,
            "place": "",
            "country": country,
            "gender": "X",
            "day": e["start"][:10],
            "endDay": e["end"][:10],          # a weather window: the contest runs on one day in it
            "url": "https://www.freerideworldtour.com" + e["link"],
            "watch": watch_key("freeride", country),
        }
        if e.get("status") and e["status"] != "scheduled":
            item["status"] = e["status"]
        events.append(item)
    return events


def season_for(now):
    """FIS seasons are named for the year they end in and turn over in summer."""
    return now.year + 1 if now.month >= 7 else now.year


SOURCE_NAMES = [f"{sector}/{category}" for sector in SECTORS for category in CATEGORIES] + ["FWT"]
SPORT_SECTOR = {sport: sector for sector, sport in SECTORS.items()}
SPORT_SECTOR["freeski"] = "FS"          # freeski events come out of the freestyle feed


def source_of(event):
    """The source an event in an earlier file came from: "AL/WC", "FS/WSC", ... or "FWT"."""
    if event.get("tour") == "FWT":
        return "FWT"
    sector = SPORT_SECTOR.get(event.get("sport"), "?")
    return f"{sector}/{'WSC' if event.get('tour') == 'WSC' else 'WC'}"


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    out.mkdir(parents=True, exist_ok=True)
    target = out / "events.json"
    now = datetime.now(timezone.utc)
    season = season_for(now)
    # Old news goes: keep from two weeks back.
    cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")

    def current(events):
        return [e for e in events if e.get("endDay", e["day"]) >= cutoff]

    # The file from the last run, when there is one: what a source that fails today falls back on.
    before = None
    if target.exists():
        try:
            before = current(json.loads(target.read_text(encoding="utf-8"))["events"])
        except Exception as e:  # noqa: BLE001 - an old file that can't be read counts as none
            print("PROBLEM the old file can't be read:", e)

    sources, problems = fis_events(season)
    try:
        sources["FWT"] = fwt_events(season)
    except Exception as e:  # noqa: BLE001
        problems.append(str(e))

    events = []
    for name in SOURCE_NAMES:
        fresh = sources.get(name)
        kept = [e for e in before or [] if source_of(e) == name]
        if kept and not fresh:
            # Couldn't be read (its problem is logged above), or came back with nothing in it.
            if fresh is not None:
                problems.append(f"{name}: no events in the feed")
            print(f"KEPT {name}: {len(kept)} events from the old file")
            events += kept
        else:
            events += fresh or []
    events = current(events)
    events.sort(key=lambda e: (e.get("start") or e["day"], SPORT_ORDER.index(e["sport"]), e["id"]))
    doc = {
        "version": 1,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season": season,
        "sports": [{"key": k, "name": SPORT_NAMES[k]} for k in SPORT_ORDER],
        "watchSets": WATCH_SETS,
        "watchNotes": WATCH_NOTES,
        "events": events,
    }
    for p in problems:
        print("PROBLEM", p)
    if before is None and problems and "--gaps-ok" not in sys.argv[2:]:
        print(f"NOT WRITTEN: {len(problems)} of {len(SOURCE_NAMES)} sources had a problem and there's no old file to fill in from")
        return 1
    if before and len(before) >= 40 and len(events) * 2 < len(before):
        print(f"NOT WRITTEN: only {len(events)} events found, and the old file has {len(before)}")
        return 1
    target.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    by = {}
    for e in events:
        by[e["sport"]] = by.get(e["sport"], 0) + 1
    print(f"WROTE {target} season {season}: {len(events)} events, {target.stat().st_size} bytes", by)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
