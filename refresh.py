#!/usr/bin/env python3
"""Reconcile a complete official crawl and build the bilingual static release."""
import argparse
import base64
import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REGIONS = {
    "asia": ("亚洲", "Asia"), "europe": ("欧洲", "Europe"), "africa": ("非洲", "Africa"),
    "north-america": ("北美洲", "North America"), "south-america": ("南美洲", "South America"),
    "oceania": ("大洋洲", "Oceania"),
}
TOPICS = {
    "fintech": ("金融科技", "Fintech", r"fin[ -]?tech|pay(?:ment|tech)|金融科技|支付"),
    "banking": ("银行", "Banking", r"\bbank(?:ing|s|er)?\b|银行"),
    "telecom": ("电信", "Telecom", r"telecom|broadband|\b5g\b|\b6g\b|fiber optic|mobile world congress|电信|通信"),
    "digital-currency": ("数字货币", "Digital Currency", r"blockchain|stablecoin|digital (?:asset|currenc)|crypto(?:currency)?|bitcoin|cbdc|web3|数字货币|区块链"),
    "packaging": ("包装", "Packaging", r"packaging|interpack|pack expo|label(?:expo|ing)|包装"),
    "identity": ("身份与安全", "Identity & Security", r"\bidentity\b|biometric|smart card|secure (?:document|credential)|身份|生物识别"),
}
# Country codes for the initial catalog; new countries may be added with bilingual geography.
CODES = dict(pair.split(':', 1) for pair in (
    "DZ:Algeria|AR:Argentina|AU:Australia|AT:Austria|BB:Barbados|BE:Belgium|BR:Brazil|CL:Chile|CN:China|"
    "CO:Colombia|EG:Egypt|ET:Ethiopia|FR:France|DE:Germany|IN:India|IT:Italy|KE:Kenya|LV:Latvia|"
    "MX:Mexico|MA:Morocco|NL:Netherlands|NZ:New Zealand|NG:Nigeria|PH:Philippines|PL:Poland|"
    "PT:Portugal|RO:Romania|SG:Singapore|ZA:South Africa|ES:Spain|CH:Switzerland|TZ:Tanzania|"
    "TH:Thailand|AE:United Arab Emirates|GB:United Kingdom|UK:United Kingdom|US:United States|"
    "USA:United States|United States of America:United States|VN:Vietnam|Viet Nam:Vietnam"
).split('|'))
EXTRA_PLACES = {
    "asia": "JP/Japan/日本|KR/South Korea/韩国|ID/Indonesia/印度尼西亚|MY/Malaysia/马来西亚|PK/Pakistan/巴基斯坦|BD/Bangladesh/孟加拉国|SA/Saudi Arabia/沙特阿拉伯|QA/Qatar/卡塔尔|BH/Bahrain/巴林|OM/Oman/阿曼|KW/Kuwait/科威特|KZ/Kazakhstan/哈萨克斯坦|IL/Israel/以色列|LK/Sri Lanka/斯里兰卡|HK/Hong Kong/中国香港|TW/Taiwan/中国台湾",
    "europe": "SE/Sweden/瑞典|NO/Norway/挪威|FI/Finland/芬兰|DK/Denmark/丹麦|IE/Ireland/爱尔兰|CZ/Czechia/捷克|GR/Greece/希腊|HU/Hungary/匈牙利|HR/Croatia/克罗地亚|LT/Lithuania/立陶宛|EE/Estonia/爱沙尼亚|LU/Luxembourg/卢森堡|RS/Serbia/塞尔维亚",
    "africa": "GH/Ghana/加纳|RW/Rwanda/卢旺达|SN/Senegal/塞内加尔|UG/Uganda/乌干达|CI/Côte d’Ivoire/科特迪瓦|MU/Mauritius/毛里求斯|TN/Tunisia/突尼斯|AO/Angola/安哥拉",
    "south-america": "PE/Peru/秘鲁|UY/Uruguay/乌拉圭|EC/Ecuador/厄瓜多尔|PY/Paraguay/巴拉圭",
    "north-america": "CA/Canada/加拿大|PA/Panama/巴拿马|CR/Costa Rica/哥斯达黎加|DO/Dominican Republic/多米尼加|JM/Jamaica/牙买加",
}

# A whole event day ends at midnight in the host city. Only use a country-wide
# default where material regional differences are absent; unmatched locations
# retain their listing through the last civil time zone.
COUNTRY_ZONE = {
    "Algeria":"Africa/Algiers","Austria":"Europe/Vienna","Barbados":"America/Barbados",
    "Belgium":"Europe/Brussels","China":"Asia/Shanghai","Colombia":"America/Bogota",
    "Egypt":"Africa/Cairo","Ethiopia":"Africa/Addis_Ababa","Germany":"Europe/Berlin",
    "India":"Asia/Kolkata","Italy":"Europe/Rome","Kenya":"Africa/Nairobi","Latvia":"Europe/Riga",
    "Morocco":"Africa/Casablanca","Netherlands":"Europe/Amsterdam","Nigeria":"Africa/Lagos",
    "Philippines":"Asia/Manila","Poland":"Europe/Warsaw","Romania":"Europe/Bucharest",
    "Rwanda":"Africa/Kigali","Singapore":"Asia/Singapore","South Africa":"Africa/Johannesburg",
    "Switzerland":"Europe/Zurich","Tanzania":"Africa/Dar_es_Salaam","Thailand":"Asia/Bangkok",
    "United Arab Emirates":"Asia/Dubai","United Kingdom":"Europe/London","Vietnam":"Asia/Ho_Chi_Minh",
    "Japan":"Asia/Tokyo","South Korea":"Asia/Seoul","Malaysia":"Asia/Kuala_Lumpur",
    "Saudi Arabia":"Asia/Riyadh","Qatar":"Asia/Qatar","Bahrain":"Asia/Bahrain","Oman":"Asia/Muscat",
    "Kuwait":"Asia/Kuwait","Sri Lanka":"Asia/Colombo","Hong Kong":"Asia/Hong_Kong","Taiwan":"Asia/Taipei",
    "Ghana":"Africa/Accra","Senegal":"Africa/Dakar","Uganda":"Africa/Kampala","Mauritius":"Indian/Mauritius",
    "Pakistan":"Asia/Karachi","Bangladesh":"Asia/Dhaka","Sweden":"Europe/Stockholm","Ireland":"Europe/Dublin",
    "Finland":"Europe/Helsinki","Czechia":"Europe/Prague","Greece":"Europe/Athens","Hungary":"Europe/Budapest",
    "Croatia":"Europe/Zagreb","Lithuania":"Europe/Vilnius","Estonia":"Europe/Tallinn","Peru":"America/Lima",
    "Uruguay":"America/Montevideo","Panama":"America/Panama","Costa Rica":"America/Costa_Rica",
}
CITY_ZONES = {
    "United States":{
        "America/New_York":"Atlanta|Brooklyn, New York|Fort Lauderdale|Hollywood, Florida|Miami|Miami Beach|National Harbor|National Harbor, Maryland|New York|New York City|Washington, D.C.|Washington, DC|Boston|Orlando",
        "America/Chicago":"Dallas|Oak Brook|Schaumburg|Chicago|Austin|Houston|New Orleans|Nashville",
        "America/Los_Angeles":"Anaheim|Chula Vista|Las Vegas|Los Angeles|San Francisco|San Diego|Seattle|San Jose|Santa Clara",
        "Pacific/Honolulu":"Honolulu","America/Denver":"Denver","America/Phoenix":"Phoenix",
    },
    "France":{"Europe/Paris":"Paris|Lyon|Cannes|Marseille"},
    "Brazil":{"America/Bahia":"Salvador","America/Sao_Paulo":"São Paulo|Rio de Janeiro"},
    "Mexico":{"America/Mexico_City":"Guadalajara|Mexico City|Ciudad de México"},
    "Australia":{"Australia/Sydney":"Canberra|Sydney","Australia/Melbourne":"Melbourne","Australia/Perth":"Perth","Australia/Brisbane":"Brisbane","Australia/Adelaide":"Adelaide"},
    "New Zealand":{"Pacific/Auckland":"Auckland|Wellington"},
    "Portugal":{"Europe/Lisbon":"Porto area|Porto|Lisbon"},
    "Spain":{"Europe/Madrid":"Barcelona|Madrid|Valencia|Bilbao"},
    "Chile":{"America/Santiago":"Santiago"},"Argentina":{"America/Argentina/Buenos_Aires":"Buenos Aires"},
    "China":{"Asia/Hong_Kong":"Hong Kong|Hong Kong SAR","Asia/Macau":"Macao|Macau"},
    "Canada":{"America/Toronto":"Toronto|Montréal|Montreal|Ottawa","America/Vancouver":"Vancouver","America/Edmonton":"Calgary|Edmonton"},
    "Indonesia":{"Asia/Jakarta":"Jakarta","Asia/Makassar":"Bali|Denpasar"},
}
FALLBACK_ZONE = "Etc/GMT+12"

STRUCTURED = {"jsonld", "ical", "microdata", "organizer_schedule"}
INACTIVE = {"EventCancelled", "EventPostponed"}
KNOWN_STATUS = INACTIVE | {"", "EventScheduled", "EventRescheduled"}
SAFE_ID = re.compile(r"[a-zA-Z0-9_-]+\Z")
YEAR = re.compile(r"\b20\d{2}\b")
SIDE_EVENT = re.compile(r"\b(?:webinar|masterclass|workshop|training|call for papers|award ceremony|networking (?:breakfast|dinner)|panel discussion|hackathon)\b", re.I)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def key(text):
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(c for c in text if c.isalnum())


def title_key(name):
    return key(YEAR.sub("", name))


def text_value(value, max_length=240):
    if not isinstance(value, str) or re.search(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        return ""
    value = " ".join(value.split())
    return value if len(value) <= max_length else ""


def host(url):
    try:
        parts = urlsplit(url)
        if parts.scheme == "https" and not parts.username and not parts.password and parts.port in (None, 443):
            return (parts.hostname or "").lower().removeprefix("www.")
    except (ValueError, TypeError):
        pass
    return ""


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected ISO calendar date")
    return date.fromisoformat(value)


def place_index(events):
    places = {}
    for e in events:
        geo = (e["countryEn"], e["country"], e["region"])
        for alias in geo[:2]:
            places[key(alias)] = geo
    for code, name in CODES.items():
        if key(name) in places:
            places[key(code)] = places[key(name)]
    for region, entries in EXTRA_PLACES.items():
        for entry in entries.split('|'):
            code, name, zh = entry.split('/')
            for alias in (code, name, zh):
                places.setdefault(key(alias), (name, zh, region))
    return places


def identity(e):
    return (title_key(e["nameEn"]), key(e["countryEn"]), key(e["cityEn"]), e["startDate"][:4])


def event_zone(e):
    country = e["countryEn"]
    city = key(e["cityEn"])
    for zone, cities in CITY_ZONES.get(country, {}).items():
        if any(city == key(alias) for alias in cities.split("|")):
            return zone
    return COUNTRY_ZONE.get(country, FALLBACK_ZONE)


def catalog_day(reference):
    if isinstance(reference, datetime):
        return reference.astimezone(ZoneInfo("Asia/Shanghai")).date()
    return reference


def in_window(e, today, until):
    if isinstance(today, datetime):
        if today.tzinfo is None:
            raise ValueError("An aware crawl timestamp is required")
        today = today.astimezone(ZoneInfo(e.get("timeZone") or event_zone(e))).date()
    return e.get("eventStatus") not in INACTIVE and day(e["endDate"]) >= today and day(e["startDate"]) <= until


def active_sources(config):
    return {s["id"]: s for s in config["sources"] if s.get("enabled", True) and s.get("official") is True}


def inspect_run(run, config):
    sources = active_sources(config)
    stats = run["sources"]
    stamp = run["lastRunAt"]
    start = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if (run["state"] != "completed" or run["sourceCount"] != len(sources)
            or len(stats) != len(sources) or {s["id"] for s in stats} != sources.keys()
            or any(abs((datetime.fromisoformat(s["checkedAt"].replace("Z", "+00:00")) - start).total_seconds()) > 86400 for s in stats)):
        raise ValueError("Crawl is incomplete or does not match the configured official sources")
    return sources, {s["id"]: s for s in stats}


def evaluate(c, source, places, stats):
    """Return a typed fact only when its source, scope and observed fields are adequate."""
    if c.get("collectedAt") != stats["checkedAt"] or stats.get("status") not in ("success", "partial"):
        return None, "not_current"
    origin = host(source["url"])
    if not origin or host(c.get("sourceUrl")) != origin or host(c.get("eventUrl") or c["sourceUrl"]) != origin:
        return None, "source_origin"
    if c.get("extractionMethod") not in STRUCTURED or not isinstance(c.get("evidence"), dict) or not c["evidence"]:
        return None, "weak_evidence"
    if c.get("recurrenceNeedsReview"):
        return None, "recurring_schedule_unresolved"
    if c["evidence"].get("superEvent") or re.search(r"/(?:sessions?|breakouts?|speakers?)/", urlsplit(c["sourceUrl"]).path, re.I):
        return None, "subevent_or_session"
    name = text_value(c.get("name"))
    city = text_value(c.get("city"), 100)
    geography = places.get(key(text_value(c.get("country"), 100)))
    if len(title_key(name)) < 4 or not city or not geography:
        return None, "name_or_location_missing"
    try:
        start, end = day(c["startDate"]), day(c["endDate"])
    except (KeyError, ValueError):
        return None, "dates_missing"
    if end < start or (end-start).days > 60 or (YEAR.findall(name) and str(start.year) not in YEAR.findall(name)):
        return None, "edition_or_date_conflict"
    status = (c.get("eventStatus") or "").rstrip("/").rsplit("/", 1)[-1]
    if status == "EventPostponed":
        # Some organizers use this flag on the replacement edition and publish
        # its confirmed dates in the same object (TOKEN2049 Dubai, for example).
        wording = " ".join(str(c.get(k) or "") for k in ("description", "sourceNote"))
        if not re.search(r"until further notice|indefinitely|new dates? (?:to be|will be) announced|延期至另行通知|新日期待定", wording, re.I):
            return None, "postponement_or_replacement_dates_unclear"
    if status not in KNOWN_STATUS:
        return None, "status_unclear"
    en, zh, region = geography
    item = dict(nameEn=name, cityEn=city, countryEn=en, country=zh, region=region,
                startDate=start.isoformat(), endDate=end.isoformat(), eventStatus=status,
                sourceUrl=c.get("eventUrl") or c["sourceUrl"], verificationMethod=c["extractionMethod"])
    return item, ""


def reconcile(catalog, config, run, candidates, today, until):
    sources, stats = inspect_run(run, config)
    events = [dict(e) for e in catalog]
    catalog_ids = {e["id"] for e in catalog}
    if any(s.get("partOfEventId") and s["partOfEventId"] not in catalog_ids for s in sources.values()):
        raise ValueError("Configured co-located event references an unknown parent event")
    places = place_index(events)
    by_identity = defaultdict(list)
    for e in events:
        by_identity[identity(e)].append(e)
    decisions, groups = [], defaultdict(list)

    def log(c, action, reason="", event_id=None):
        decisions.append({"candidateId": c.get("id"), "sourceId": c.get("sourceId"), "name": c.get("name"),
                          "action": action, "reason": reason, "eventId": event_id})

    for c in candidates:
        sid = c.get("sourceId")
        if sid not in sources:
            log(c, "pending", "unknown_source")
            continue
        if sources[sid].get("partOfEventId"):
            log(c, "pending", "part_of_listed_fair", sources[sid]["partOfEventId"])
            continue
        item, reason = evaluate(c, sources[sid], places, stats[sid])
        if reason:
            log(c, "skipped" if reason == "not_current" else "pending", reason)
            continue
        groups[identity(item)].append((c, item))
    possible_neighbors = defaultdict(list)
    for batch in groups.values():
        for _, fact in batch:
            place = (host(fact["sourceUrl"]), key(fact["countryEn"]), key(fact["cityEn"]))
            possible_neighbors[place].append(fact)
    for ident, batch in sorted(groups.items()):
        def record_batch(action, reason, event_id=None):
            for raw, _ in batch:
                log(raw, action, reason, event_id)

        facts = {(e["startDate"], e["endDate"], e["eventStatus"] in INACTIVE) for _, e in batch}
        if len(facts) != 1 or len(by_identity[ident]) > 1:
            record_batch("pending", "conflicting_or_ambiguous_edition")
            continue
        c, item = batch[0]
        matches = by_identity[ident]
        old = matches[0] if matches else None
        source = sources[c["sourceId"]]
        # Same title at another venue/date or a differently named record sharing a
        # detail page could be a rename or a co-located event. Keep the evidence.
        conflict = any(e is not old and (
            (title_key(e["nameEn"]) == ident[0] and e["startDate"][:4] == ident[3]
             and e["startDate"] <= item["endDate"] and e["endDate"] >= item["startDate"])
            or (e["sourceUrl"].rstrip("/") == item["sourceUrl"].rstrip("/")
                and e["startDate"] <= item["endDate"] and e["endDate"] >= item["startDate"]))
            for e in events)
        place = (host(item["sourceUrl"]), ident[1], ident[2])
        other_titles = any(identity(fact) != ident and fact["startDate"] <= item["endDate"]
                           and fact["endDate"] >= item["startDate"] for fact in possible_neighbors[place])
        existing_neighbor = any((host(e["sourceUrl"]), key(e["countryEn"]), key(e["cityEn"])) == place
                                and e["startDate"] <= item["endDate"] and e["endDate"] >= item["startDate"]
                                for e in events)
        if not old and (conflict or other_titles or existing_neighbor):
            record_batch("pending", "possible_duplicate")
            continue
        if old and host(old["sourceUrl"]) not in {host(e["sourceUrl"]) for _, e in batch}:
            record_batch("pending", "different_publisher")
            continue
        if not old and (not in_window(item, today, until) or SIDE_EVENT.search(item["nameEn"])):
            record_batch("skipped", "outside_window_or_session")
            continue
        if old:
            # A canceled event cannot be reinstated by an unspecified status.
            if old.get("eventStatus") in INACTIVE and not item["eventStatus"]:
                record_batch("pending", "reinstatement_unconfirmed")
                continue
            changed = any(old.get(k, "") != item[k] for k in ("startDate", "endDate"))
            if changed:
                # Organizers can embed stale schema (e.g. PAY360 listed May in its
                # homepage JSON-LD when its published delegate guide said April).
                # Ignore fragments and query variants of the same evidence page.
                evidence_pages = {(host(raw["sourceUrl"]), urlsplit(raw["sourceUrl"]).path.rstrip("/"))
                                  for raw, _ in batch}
                formats = {raw["extractionMethod"] for raw, _ in batch}
                # An entire site can repeat one erroneous header JSON-LD block.
                # Distinct paths alone do not establish independent corroboration.
                independent = len(evidence_pages) >= 2 and len(formats) >= 2
                direct_schedule = any(raw["extractionMethod"] == "organizer_schedule" and
                                      raw["evidence"].get("sourcePassage") for raw, _ in batch)
                if not independent and not direct_schedule:
                    record_batch("pending", "date_change_needs_corroboration", old["id"])
                    continue
            status = item["eventStatus"]
            changed |= bool(status and (status in INACTIVE or old.get("eventStatus") in INACTIVE) and old.get("eventStatus") != status)
            if changed:
                old.update(startDate=item["startDate"], endDate=item["endDate"], eventStatus=status, sourceUrl=item["sourceUrl"])
                set_provenance(old, item, catalog_day(today))
                # Curated prose can cite the previous date or imply an unconfirmed schedule.
                old["description"], old["descriptionEn"] = descriptions(old)
            old["lastConfirmedAt"] = catalog_day(today).isoformat()
            log(c, "updated" if changed else "confirmed", event_id=old["id"])
        else:
            name = item["nameEn"]
            exact_series = source.get("sourceType") != "discovery" and title_key(source.get("nameEn", "")) == ident[0]
            topics = [t for t, (_, _, pattern) in TOPICS.items() if re.search(pattern, name, re.I)]
            if exact_series:
                topics = list(dict.fromkeys(topics + [t for t in source.get("topics", []) if t in TOPICS]))
            if not topics:
                record_batch("pending", "topic_unconfirmed")
                continue
            # KuppingerCole lists its own standalone Impact Day conferences under
            # edition-specific URLs; a session or directory page is insufficient.
            impact_day = (host(item["sourceUrl"]) == "kuppingercole.com" and
                          re.fullmatch(r"/events/[a-z0-9-]+id20\d{2}/?", urlsplit(item["sourceUrl"]).path, re.I)
                          and re.search(r"\bImpact Day(?: 20\d{2})?$", name, re.I))
            if not exact_series and not impact_day and not re.search(r"festival|expo|forum|summit|conference|congress|convention|exhibition|fair|week|展|大会|峰会|论坛", name, re.I):
                record_batch("pending", "event_type_unconfirmed")
                continue
            stable_id = "auto-" + hashlib.sha256(compact(ident).encode()).hexdigest()[:18]
            zh_city = next((e["city"] for e in events if key(e["cityEn"]) == ident[2]
                            and key(e["countryEn"]) == ident[1]), item["cityEn"])
            item.update(id=stable_id, name=name, city=zh_city, topics=topics, participants="", verification="official")
            if exact_series and source.get("name"):
                item["name"] = YEAR.sub(ident[3], source["name"])
            if impact_day:
                names = {"customeridentityaccessmanagementciamimpactday": "客户身份与访问管理（CIAM）主题日",
                         "nordicsidentityimpactday": "北欧数字身份主题日"}
                item["name"] = names.get(ident[0], name)
                if ident[2] == "gothenburg":
                    item["city"] = "哥德堡"
            item["description"], item["descriptionEn"] = descriptions(item)
            venue = text_value(c.get("venue"), 160)
            if venue:
                item["description"] += f" 会场：{venue}。"
                item["descriptionEn"] += f" Venue: {venue}."
            set_provenance(item, item, catalog_day(today))
            events.append(item)
            by_identity[ident].append(item)
            log(c, "added", event_id=stable_id)
        for extra, _ in batch[1:]:
            log(extra, "duplicate", event_id=by_identity[ident][0]["id"])
    if len(decisions) != len(candidates):
        raise ValueError("Each collected record must have a reconciliation decision")
    return events, decisions


def descriptions(e):
    return (f'{e["name"]}，举办地为{e["country"]}·{e["city"]}，详情见主办方页面。',
            f'{e["nameEn"]} takes place in {e["cityEn"]}, {e["countryEn"]}. See the organiser’s page for details.')


def set_provenance(e, item, today):
    e.update(verifiedAt=today.isoformat(), verificationMethod=item["verificationMethod"],
             sourceNote="自动采集自官网公布的展会日程、地点和名称。", sourceNoteEn="Dates, location and title were collected automatically from the official event listing.")

SOURCE_FILES = ("index.html", "styles.css", "translations.js", "app.js", "assets/world.svg",
                "assets/favicon.svg", "data/verified-events.json", "data/events.json", "data/collection-status.json")
PREFIX = "exhibition-atlas/"
RELEASE = ("index.html", "events-zh.csv", "events-en.csv", "exhibition-source.zip")


def source_tree(root):
    tree = {}
    with ZipFile(root / "exhibition-source.zip") as archive:
        for name in SOURCE_FILES:
            tree[name] = archive.read(PREFIX + name)
        labels_path = "data/company-labels.json"
        if PREFIX + labels_path in archive.namelist():
            tree[labels_path] = archive.read(PREFIX + labels_path)
    # An extracted development checkout may supply revised UI or curated records.
    for name in (*SOURCE_FILES, labels_path):
        path = root / "site" / name
        if path.is_file():
            tree[name] = path.read_bytes()
    return tree


def validate_catalog(events):
    used_ids = set()
    for e in events:
        required = ("name", "nameEn", "country", "countryEn", "city", "cityEn",
                    "description", "descriptionEn", "sourceNote", "sourceNoteEn", "sourceUrl")
        if not all(isinstance(e.get(k), str) and e[k].strip() for k in required):
            raise ValueError(f'Missing bilingual content for {e.get("id")}')
        if not SAFE_ID.fullmatch(e.get("id", "")) or e["id"] in used_ids:
            raise ValueError(f'Invalid or repeated event ID {e.get("id")}')
        if not host(e["sourceUrl"]) or day(e["endDate"]) < day(e["startDate"]) or e["region"] not in REGIONS:
            raise ValueError(f'Invalid source/date/region for {e["id"]}')
        if not e.get("topics") or set(e["topics"]) - TOPICS.keys() or e.get("verification") != "official":
            raise ValueError(f'Invalid topic or verification for {e["id"]}')
        day(e["verifiedAt"])
        used_ids.add(e["id"])


def company_export(event, en, labels=None):
    relationships = {"peer": ("同行", "Peer"), "supplier": ("供应商", "Supplier"),
                     "both": ("同行及供应商", "Peer & supplier"), "watchlist": ("清单企业", "Listed company")}
    roles = {"attendee": ("参会机构", "Attending organization"), "exhibitor": ("参展商", "Exhibitor"), "sponsor": ("赞助商", "Sponsor"),
             "speaker": ("讲者所属机构", "Speaker affiliation"), "organizer": ("主办方", "Organizer"),
             "partner": ("展会合作方", "Event partner")}
    lines = []
    for m in event.get("companyMatches", []):
        if m.get("relationship") not in relationships or m.get("role") not in roles:
            continue
        official = m["name"]
        display = (labels or {}).get(m["companyId"], {}).get("en" if en else "zh", official)
        names = official if display == official else f"{display} ({official})"
        lines.append(" · ".join([names, relationships[m["relationship"]][en],
                                 roles[m["role"]][en], m["checkedAt"], m["sourceUrl"]]))
    return "\n".join(lines)


def csv_export(events, en, labels=None):
    f = io.StringIO(newline="")
    w = csv.writer(f)
    header = ("Event,Region,Country,City,Start date,End date,Topics,Official website,Overview,Verification notes,Checked on" if en
              else "展会名称,大区,国家,城市,开始日期,结束日期,主题,官网链接,简介,核验说明,核验日期")
    w.writerow(header.split(",") + ["Companies, roles and sources" if en else "参与企业、角色及来源"])
    suffix = "En" if en else ""
    for e in events:
        row = [e["name"+suffix], REGIONS[e["region"]][en], e["country"+suffix], e["city"+suffix],
               e["startDate"], e["endDate"], " / ".join(TOPICS[t][en] for t in e["topics"]),
               e["sourceUrl"], e["description"+suffix], e["sourceNote"+suffix], e["verifiedAt"], company_export(e, en, labels)]
        w.writerow(["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) or v.startswith(("\t", "\r", "\n")) else v for v in row])
    return f.getvalue().encode("utf-8-sig")


def confirmed_labels(rows, available):
    public = {}
    for event in rows:
        for match in event.get("companyMatches", []):
            cid = match["companyId"]
            labels = available.get(cid, {})
            if isinstance(labels, dict):
                pair = {k: labels[k] for k in ("zh", "en") if isinstance(labels.get(k), str) and labels[k].strip()}
                if pair:
                    public[cid] = pair
    return public


def make_bundle(meta, rows, world, status, labels=None):
    data = compact(dict(meta=meta, events=rows, worldMap=world, collectionStatus=status, companyLabels=labels or {}))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return ("window.ATLAS_DATA=" + data + ";\n").encode()


def single_page(tree):
    page = tree["index.html"].decode()

    def replace(pattern, value):
        nonlocal page
        page, count = re.subn(pattern, lambda _: value, page)
        if count != 1:
            raise ValueError(f"Unexpected source template for {pattern}")
    replace(r'href="assets/favicon\.svg"', 'href="data:image/svg+xml;base64,' + base64.b64encode(tree["assets/favicon.svg"]).decode() + '"')
    replace(r'<link rel="stylesheet" href="styles\.css[^"]*">', "<style>\n" + tree["styles.css"].decode() + "\n</style>")
    scripts = []
    for name in ("data/bundle.js", "translations.js", "app.js"):
        replace(r'<script src="' + re.escape(name) + r'(?:\?[^"]*)?" defer></script>', "")
        code = tree[name].decode()
        if re.search(r"</script", code, re.I):
            raise ValueError(f"Unsafe literal script termination in {name}")
        scripts.append("<script>\n" + code + "\n</script>\n")
    replace(r"</body>", "".join(scripts) + "</body>")
    return page.encode()


def archive_bytes(tree):
    buf = io.BytesIO()
    with ZipFile(buf, "w") as archive:
        for path, contents in sorted(tree.items()):
            entry = ZipInfo(PREFIX + path, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, contents, compress_type=ZIP_DEFLATED)
    return buf.getvalue()


def verify_outputs(output, rows):
    with ZipFile(io.BytesIO(output["exhibition-source.zip"])) as archive:
        bundle = archive.read(PREFIX + "data/bundle.js")
    if bundle not in output["index.html"]:
        raise ValueError("The offline and online bundles differ")
    for filename in ("events-zh.csv", "events-en.csv"):
        actual = list(csv.reader(io.StringIO(output[filename].decode("utf-8-sig"))))[1:]
        if len(actual) != len(rows) or [(r[4], r[5], r[7]) for r in actual] != [(e["startDate"], e["endDate"], e["sourceUrl"]) for e in rows]:
            raise ValueError(f"CSV and site differ in {filename}")


def stage_writes(items):
    staged = []
    try:
        for path, contents in items:
            if path.is_file() and path.read_bytes() == contents:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                tmp = Path(f.name)
                staged.append((tmp, path))
                f.write(contents)
            tmp.chmod(0o644)
        for tmp, path in staged:
            os.replace(tmp, path)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


def build(root, input_dir, today, dry_run=False):
    tree = source_tree(root)
    curated = json.loads(tree["data/verified-events.json"])
    imported = json.loads(tree["data/events.json"])
    meta = imported["meta"]
    until = day(meta["windowEnd"])
    config = json.loads((root / "sources.json").read_bytes())
    catalog = curated["events"] + imported["events"]
    validate_catalog(catalog)
    status = json.loads(tree["data/collection-status.json"])
    report = None
    if input_dir:
        run = json.loads((input_dir / "collection-status.json").read_bytes())
        candidates = json.loads((input_dir / "review-queue.json").read_bytes())["candidates"]
        fingerprint = hashlib.sha256(compact([config, run, candidates]).encode()).hexdigest()
        previous = input_dir / "publication-report.json"
        if previous.is_file() and status.get("lastRunAt") == run["lastRunAt"]:
            saved = json.loads(previous.read_bytes())
            if (saved.get("runFingerprint") == fingerprint and
                    status.get("runFingerprint") == fingerprint):
                report = saved
        if report is not None:
            # A retry of the same evidence should keep its original audit
            # classifications; otherwise the added records would become confirmations.
            events = catalog
        else:
            events, decisions = reconcile(catalog, config, run, candidates, today, until)
            counts = Counter(d["action"] for d in decisions)
            by_status = Counter(s["status"] for s in run["sources"])
            fetched = sum(by_status[k] for k in ("success", "manual_review", "partial"))
            status = dict(state="completed", mode="automatic", scheduled=True, sourceCount=run["sourceCount"],
                          lastRunAt=run["lastRunAt"], okCount=fetched, failedCount=run["sourceCount"] - fetched,
                          sourcesWithCandidates=by_status["success"] + by_status["partial"],
                          sourcesWithoutParsedEvents=by_status["manual_review"], statusCounts=dict(by_status),
                          pendingCount=counts["pending"], publishedNew=counts["added"], updatedCount=counts["updated"],
                          confirmedCount=counts["confirmed"])
            report = {"lastRunAt": run["lastRunAt"], "counts": dict(counts), "decisions": decisions}
            status["runFingerprint"] = report["runFingerprint"] = fingerprint
    else:
        events = catalog
    if input_dir and (input_dir / "company-evidence.json").is_file() and (root / "company-watchlist.json").is_file():
        import company_capture
        company_capture.apply_snapshot(
            events, json.loads((root / "company-watchlist.json").read_bytes()),
            json.loads((input_dir / "company-evidence.json").read_bytes()), run["lastRunAt"])
    for e in events:
        e["timeZone"] = event_zone(e)
    validate_catalog(events)
    rows = sorted((e for e in events if in_window(e, today, until)), key=lambda e: (e["startDate"], e["nameEn"], e["id"]))
    available_labels = json.loads(tree.get("data/company-labels.json", b"{}"))
    if input_dir and (input_dir / "company-labels.json").is_file():
        available_labels.update(json.loads((input_dir / "company-labels.json").read_bytes()))
    labels = confirmed_labels(rows, available_labels)
    if input_dir:
        status["activeCount"] = len(rows)
        report["activeCount"] = len(rows)
    meta.update(checkedAt=catalog_day(today).isoformat(), note="按举办地归类，收录进行中和即将举行的展会。",
                noteEn="Current and upcoming events grouped by host location.")
    curated["events"] = events
    curated["collectionWindow"] = dict(asOf=catalog_day(today).isoformat(), endDate=until.isoformat(),
                                      rule="Include through the end of the closing day in the host time zone and within the opening-date horizon.")
    imported["events"] = []
    tree.update({"data/events.json": json_bytes(imported), "data/verified-events.json": json_bytes(curated),
                 "data/sources.json": json_bytes(config), "data/collection-status.json": json_bytes(status),
                 "data/company-labels.json": json_bytes(labels),
                 "data/bundle.js": make_bundle(meta, rows, tree["assets/world.svg"].decode(), status, labels)})
    # Include the same release implementation in the source download.
    for name in ("collector.py", "refresh.py", "test_collector.py", "test_refresh.py", "company_capture.py", "test_company_capture.py", "company-sources.json", "README.md"):
        if (root / name).is_file():
            tree[name] = (root / name).read_bytes()
    tree[".github/workflows/collect.yml"] = (root / ".github/workflows/collect.yml").read_bytes()
    output = {"index.html": single_page(tree), "events-zh.csv": csv_export(rows, False, labels),
              "events-en.csv": csv_export(rows, True, labels), "exhibition-source.zip": archive_bytes(tree)}
    verify_outputs(output, rows)
    if not dry_run:
        writes = [(root / name, value) for name, value in output.items()]
        if (root / "site").is_dir():
            writes.extend((root / "site" / name, tree[name]) for name in tree if name.startswith("data/"))
        if report is not None:
            writes.append((input_dir / "publication-report.json", json_bytes(report)))
        stage_writes(writes)
    return {"asOf": str(catalog_day(today)), "catalog": len(events), "active": len(rows), "dryRun": dry_run,
            "decisions": report["counts"] if report else {}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="Output directory of the completed collector")
    mode.add_argument("--build-only", action="store_true", help="Rebuild current records without collecting")
    parser.add_argument("--as-of", type=day, help="Use a fixed local calendar day for a deterministic replay")
    parser.add_argument("--dry-run", action="store_true", help="Validate and build in memory without writing")
    args = parser.parse_args()
    today = args.as_of or datetime.now(timezone.utc)
    print(compact(build(ROOT, args.input, today, args.dry_run)))


if __name__ == "__main__":
    main()
