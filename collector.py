#!/usr/bin/env python3
"""Collect official event evidence from configured organizers and their event calendars.

Python 3.9+, standard library only. Published events.json is never modified.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "ExhibitionAtlasBot/0.2"
MAX_BYTES = 2 * 1024 * 1024
EVENT_TYPES = {
    "Event",
    "BusinessEvent",
    "ExhibitionEvent",
    "EducationEvent",
    "ConferenceEvent",
    "SocialEvent",
    "Festival",
}
REGIONS = {"asia", "europe", "africa", "north-america", "south-america", "oceania"}
TOPICS = {"fintech", "banking", "telecom", "digital-currency", "packaging", "identity"}


def timestamp():
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def safe_url(url):
    """Allow only credential-free public HTTPS URLs on the default TLS port."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only credential-free HTTPS source URLs are allowed")
    if parsed.port not in (None, 443):
        raise ValueError("Only HTTPS port 443 is allowed")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local hosts are not allowed")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_global:
            raise ValueError("Private or special-use addresses are not allowed")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def origin(url):
    parsed = urlsplit(url)
    return (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)


def public_addresses(host):
    addresses = list(
        dict.fromkeys(
            item[4][0]
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        )
    )
    if not addresses or any(
        not ipaddress.ip_address(value).is_global for value in addresses
    ):
        raise ValueError("DNS resolved to a private, local, or special-use address")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to checked public IPs while preserving TLS hostname verification."""

    def connect(self):
        addresses = public_addresses(self.host)
        last_error = None
        for address in addresses:
            raw = None
            try:
                raw = socket.create_connection((address, self.port), self.timeout)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as exc:
                if raw:
                    raw.close()
                last_error = exc
        raise OSError("Unable to connect to the verified public host") from last_error


class Fetcher:
    def __init__(self, timeout=15, delay=2):
        self.timeout = timeout
        self.delay = max(2, delay)
        self.last_request = {}

    def get(self, url, delay=None, robots=None):
        initial_origin = origin(safe_url(url))
        for _ in range(6):
            url = safe_url(url)
            if origin(url) != initial_origin:
                raise ValueError(
                    "Cross-origin redirect blocked; review and configure the canonical official URL"
                )
            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                raise ValueError("Redirect destination is disallowed by robots.txt")
            parsed = urlsplit(url)
            wait = max(self.delay, delay or 0) - (
                time.monotonic() - self.last_request.get(initial_origin, 0)
            )
            if wait > 0:
                time.sleep(wait)
            connection = PinnedHTTPSConnection(
                parsed.hostname,
                443,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
            self.last_request[initial_origin] = time.monotonic()
            try:
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,text/calendar,application/ld+json,application/json,text/plain;q=0.8",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                headers = {key.lower(): value for key, value in response.getheaders()}
                if response.status in {301, 302, 303, 307, 308}:
                    target = headers.get("location")
                    if not target:
                        raise ValueError("Redirect without Location")
                    url = urljoin(url, target)
                    continue
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("Response exceeds the 2 MiB collection limit")
                encoding = re.search(
                    r"charset=([\w-]+)", headers.get("content-type", ""), re.I
                )
                charset = encoding.group(1) if encoding else "utf-8"
                try:
                    text = body.decode(charset, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return {
                    "status": response.status,
                    "headers": headers,
                    "body": text,
                    "url": url,
                }
            finally:
                connection.close()
        raise ValueError("Too many redirects")


class JsonLDParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks = []
        self.collect = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            kind = (dict(attrs).get("type") or "").split(";")[0].strip().lower()
            self.collect = kind == "application/ld+json"
            self.parts = []

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.collect:
            self.blocks.append("".join(self.parts))
            self.collect = False


def walk_json_objects(value):
    """Traverse @graph, ItemList, nested itemListElement and ordinary arrays."""
    if isinstance(value, list):
        for item in value:
            yield from walk_json_objects(item)
    elif isinstance(value, dict):
        yield value
        for key, item in value.items():
            if key != "@context" and isinstance(item, (dict, list)):
                yield from walk_json_objects(item)


def event_objects(value):
    nodes = list(walk_json_objects(value))
    by_id = {
        node["@id"]: node
        for node in nodes
        if isinstance(node.get("@id"), str) and len(node) > 1
    }

    parents_by_node, parents_by_id = {}, {}
    for node in nodes:
        for field in ("subEvent", "subEvents"):
            if field not in node:
                continue
            children = node[field] if isinstance(node[field], list) else [node[field]]
            parent = {
                key: node[key] for key in ("@id", "name", "url") if node.get(key)
            } or {"@type": "Event"}
            for child in children:
                if isinstance(child, dict):
                    parents_by_node[id(child)] = parent
                    identifier = child.get("@id")
                else:
                    identifier = child
                if isinstance(identifier, str):
                    parents_by_id[identifier] = parent

    def resolve_location(node, depth=0):
        if isinstance(node, list):
            return [resolve_location(item, depth) for item in node]
        if not isinstance(node, dict) or depth > 4:
            return node
        identifier = node.get("@id")
        resolved = (
            dict(by_id.get(identifier, {})) if isinstance(identifier, str) else {}
        )
        resolved.update(node)
        for field in ("address", "addressCountry", "addressLocality"):
            if field in resolved:
                resolved[field] = resolve_location(resolved[field], depth + 1)
        return resolved

    for node in nodes:
        kinds = node.get("@type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        kinds = {
            str(kind).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for kind in kinds
        }
        if kinds & EVENT_TYPES:
            event = dict(node)
            if not event.get("superEvent"):
                parent = parents_by_node.get(id(node))
                identifier = node.get("@id")
                if not parent and isinstance(identifier, str):
                    parent = parents_by_id.get(identifier)
                if parent:
                    event["superEvent"] = parent
            if "location" in node:
                event["location"] = resolve_location(node["location"])
            yield event


def extract_events(document):
    parser = JsonLDParser()
    parser.feed(document)
    blocks, failures = [], 0
    for block in parser.blocks:
        try:
            blocks.append(json.loads(block))
        except (ValueError, TypeError, RecursionError):
            failures += 1
    try:
        return list(event_objects(blocks)), failures
    except RecursionError:
        return [], failures + 1


class CalendarLinks(HTMLParser):
    """Collect navigation links and short visible date passages from an official page."""

    void = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__()
        self.links = []
        self.anchor = None
        self.ignored = 0
        self.visible = []
        self.microdata = []
        self.scopes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag in {"a", "link"} and attrs.get("href"):
            link = [
                attrs["href"],
                attrs.get("type", "") or "",
                attrs.get("aria-label", "") or "",
            ]
            self.links.append(link)
            if tag == "a":
                self.anchor = link
        prop = (attrs.get("itemprop") or "").split()
        field = prop[0] if prop else ""
        is_scope = "itemscope" in attrs
        parent = self.scopes[-1] if self.scopes else None
        scope = None
        if is_scope:
            scope = {"@type": (attrs.get("itemtype") or "").split("/")[-1]}
            if parent is not None and field:
                parent.setdefault(field, scope)
            if scope["@type"] in EVENT_TYPES:
                self.microdata.append(scope)
            self.scopes.append(scope)
        value = attrs.get("content") or attrs.get("datetime") or attrs.get("href")
        if not is_scope and parent is not None and field and value:
            parent.setdefault(field, value)
        if tag not in self.void:
            self.stack.append(
                (tag, scope, parent if not is_scope and not value else None, field, [])
            )
        elif is_scope:
            self.scopes.pop()

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in {"script", "style", "noscript"}:
                self.ignored -= 1
            return
        if tag == "a":
            self.anchor = None
        if tag in self.void:
            return
        # Ignore malformed unmatched closers without deleting other valid scopes.
        if not any(frame[0] == tag for frame in reversed(self.stack)):
            return
        while self.stack:
            opened, scope, parent, field, parts = self.stack.pop()
            if parent is not None and field and parts:
                parent.setdefault(field, " ".join(parts))
            if scope is not None and self.scopes:
                self.scopes.pop()
            if opened == tag:
                break

    def handle_data(self, data):
        if self.ignored or not data.strip():
            return
        data = " ".join(data.split())
        self.visible.append(data)
        if self.anchor is not None:
            self.anchor[2] += " " + data
        for _, _, parent, field, parts in self.stack[-8:]:
            if field and parent is not None:
                parts.append(data)


def calendar_date(value):
    if not re.fullmatch(r"\d{8}(?:T\d{6}Z?)?", value):
        return ""
    return iso_date(value[:4] + "-" + value[4:6] + "-" + value[6:8])


def extract_ical(document):
    """Read VEVENT only, unfold lines and honor RFC 5545 non-inclusive all-day DTEND."""
    document = re.sub(r"\r?\n[ \t]", "", document)
    records = []
    event = None
    nested = 0
    for line in document.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.split(";", 1)[0].upper()
        if key == "BEGIN" and value.upper() == "VEVENT":
            event = {}
            nested = 0
        elif event is not None and key == "BEGIN":
            nested += 1
        elif event is not None and key == "END" and nested:
            nested -= 1
        elif event is not None and key == "END" and value.upper() == "VEVENT":
            start = calendar_date(event.get("DTSTART", ""))
            end = calendar_date(event.get("DTEND", ""))
            if end and re.fullmatch(r"\d{8}", event.get("DTEND", "")):
                end = (dt.date.fromisoformat(end) - dt.timedelta(days=1)).isoformat()
            if (
                not end
                and start
                and "DTEND" not in event
                and "DURATION" not in event
                and len(event["DTSTART"]) == 8
            ):
                end = start
            record = {
                "@type": "Event",
                "@id": event.get("UID", ""),
                "name": event.get("SUMMARY", ""),
                "startDate": start,
                "endDate": end,
                "url": event.get("URL", ""),
                "location": {"name": event.get("LOCATION", "")},
                "eventStatus": event.get("STATUS", ""),
                "description": event.get("DESCRIPTION", ""),
            }
            if event.get("RRULE") or event.get("RECURRENCE-ID"):
                record["recurrence"] = True
            records.append(record)
            event = None
        elif event is not None and not nested:
            event[key] = re.sub(
                r"\\([nN,;\\])",
                lambda m: " " if m[1].lower() == "n" else m[1],
                value.strip(),
            )
    return records


def same_origin_url(base, href):
    if not isinstance(href, str) or not href.strip() or href.startswith("#"):
        return ""
    try:
        url = safe_url(urljoin(base, href.strip()))
        return url if origin(url) == origin(base) else ""
    except (ValueError, TypeError):
        return ""


def discovery_links(parser, base, organizer_calendar=False):
    ranked, external = {}, {}
    for href, kind, label in parser.links:
        if not href or href.startswith("#"):
            continue
        try:
            url = safe_url(urljoin(base, href.strip()))
        except (TypeError, ValueError):
            continue
        if url == base:
            continue
        parsed = urlsplit(url)
        target = (parsed.path + " " + parsed.query + " " + label).lower()
        if re.search(
            r"register|registration|login|logout|tickets|account|mailto|google.calendar|\.(?:pdf|zip|jpe?g|png|svg|docx?)\b",
            target,
        ):
            continue
        score = 0
        if "text/calendar" in kind.lower() or re.search(
            r"\.ics(?:$|[?&#/ ])|icalendar|add.to.calendar|download.calendar", target
        ):
            score = 4
        elif re.search(
            r"(?:upcoming|all|future|our)[ /_-]+(?:events|shows|exhibitions|conferences)|event[ /_-]*calendar",
            target,
        ):
            score = 3
        elif re.search(
            r"(?:event|summit|festival|conference|expo|trade.show|exhibition|packag).{0,65}20\d{2}|20\d{2}.{0,65}(?:event|summit|festival|conference|expo|exhibition)",
            target,
        ):
            score = 2
        elif re.search(
            r"/(?:events|calendar|exhibitions|conferences)(?:/[^/.]+)?/?$",
            parsed.path,
            re.I,
        ) or re.search(
            r"\b(?:packaging|fintech|banking|identity|telecom|payment[s]?|digital currency) (?:week|festival|summit|show|fair|expo|conference)\b",
            label,
            re.I,
        ):
            score = 1
        if score and origin(url) == origin(base):
            ranked[url] = max(score, ranked.get(url, 0))
        elif origin(url) != origin(base):
            # Show organizer-linked sister exhibitions even when the banner label is only a city name.
            host = parsed.hostname or ""
            external_home = (
                organizer_calendar
                and parsed.path in ("", "/")
                and bool(plain(label, 160))
                and not re.search(
                    r"facebook|instagram|linkedin|twitter|youtube|google|youtu\.be|x\.com$|tiktok",
                    host,
                    re.I,
                )
            )
            if score or external_home:
                external.setdefault(url, {"url": url, "label": plain(label, 160)})
    return sorted(ranked, key=lambda url: -ranked[url]), list(external.values())


def rotate_links(links, budget, checked_at):
    """Cover large link lists across scheduled days while keeping a high-priority entry."""
    if budget <= 0 or len(links) <= budget:
        return links
    date = dt.date.fromisoformat(checked_at[:10])
    keep = min(1, budget - 1)
    rotating = links[keep:]
    offset = date.toordinal() * (budget - keep) % len(rotating)
    return links[:keep] + rotating[offset:] + rotating[:offset]


def date_evidence(text):
    # This is only a lead for review; date-free-year snippets never establish an edition.
    months = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    day_range = r"\d{1,2}(?:st|nd|rd|th)?(?:\s*[-–—]\s*\d{1,2}(?:st|nd|rd|th)?)?"
    pattern = rf"(?:{day_range}\s+{months}\.?[, ]+20\d{{2}}|{months}\.?\s+{day_range},?\s+20\d{{2}}|20\d{{2}}[-年/]\d{{1,2}}[-月/]\d{{1,2}}日?)"
    return list(dict.fromkeys(match[0] for match in re.finditer(pattern, text, re.I)))[
        :8
    ]


def singapore_festival_schedule(url, visible_text):
    """Read the main expo dates separately from the SFF invitation-only lead-in."""
    page = urlsplit(url)
    if page.hostname not in {"www.fintechfestival.sg", "fintechfestival.sg"}:
        return []
    if page.path.rstrip("/") not in {"", "/festival-overview"}:
        return []
    month = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    if page.path.rstrip("/"):
        edition = re.search(r"\bSFF (20\d{2}) Festival Overview\b", visible_text, re.I)
        dates = re.search(
            r"Singapore FinTech Festival at Singapore EXPO\s+"
            r"(?:[A-Za-z]+\s+)?(\d{1,2})\s*(?:[-–—]|to)\s*(?:[A-Za-z]+\s+)?(\d{1,2})\s+"
            + month,
            visible_text,
            re.I,
        )
    else:
        edition = re.search(
            r"Singapore FinTech Festival\s+(20\d{2})\b", visible_text, re.I
        )
        dates = re.search(
            r"\b(\d{1,2})\s*(?:[-–—]|to)\s*(\d{1,2})\s+"
            + month
            + r"\s+(20\d{2})\s*\|\s*Singapore EXPO\b",
            visible_text,
            re.I,
        )
        if dates and edition and dates[4] != edition[1]:
            return []
    if not edition or not dates:
        return []
    try:
        year = int(edition[1])
        month_number = dt.datetime.strptime(dates[3].title(), "%B").month
        first = dt.date(year, month_number, int(dates[1]))
        last = dt.date(year, month_number, int(dates[2]))
    except ValueError:
        return []
    if last < first:
        return []
    return [
        {
            "@type": "Festival",
            "name": f"Singapore FinTech Festival {year}",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "url": url,
            "location": {
                "name": "Singapore EXPO",
                "address": {
                    "addressLocality": "Singapore",
                    "addressCountry": "Singapore",
                },
            },
            "sourcePassage": edition[0] + " | " + dates[0],
        }
    ]


def organizer_faq_schedule(url, visible_text):
    page = urlsplit(url)
    host = page.hostname
    path = page.path.rstrip("/")
    day_range = r"(\d{1,2})\s*(?:&|[-–—]|to)\s*(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})"
    if (
        host == "india.blackswansummit.com"
        and path == "/faqs"
        and "Black Swan Summit India" in visible_text
    ):
        pattern = (
            r"When and where is the Summit\?\s+"
            + day_range
            + r"\s+at (Mayfair Sanctuary), (Bhubaneswar), Odisha\b"
        )
        name, country = "Black Swan Summit India", "India"
    elif (
        host in {"www.pay360event.com", "pay360event.com"}
        and path in {"", "/faqs"}
        and re.search(r"\bPAY360\b", visible_text)
    ):
        pattern = day_range + r"\s+(ExCeL),\s*(London),\s*(?:UK|United Kingdom)\b"
        name, country = "PAY360", "United Kingdom"
    else:
        return []
    match = re.search(pattern, visible_text, re.I)
    if not match:
        return []
    try:
        month = dt.datetime.strptime(match[3].title(), "%B").month
        first = dt.date(int(match[4]), month, int(match[1]))
        last = dt.date(int(match[4]), month, int(match[2]))
    except ValueError:
        return []
    if last < first:
        return []
    return [
        {
            "@type": "BusinessEvent",
            "name": f"{name} {first.year}",
            "url": url,
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "location": {
                "name": match[5],
                "address": {"addressLocality": match[6], "addressCountry": country},
            },
            "sourcePassage": match[0],
        }
    ]


def page_records(result, source, checked_at):
    url, body = result["url"], result["body"]
    content_type = result.get("headers", {}).get("content-type", "").lower()
    links, failures, hints, external = [], 0, [], []
    if "text/calendar" in content_type or body.lstrip().upper().startswith(
        "BEGIN:VCALENDAR"
    ):
        raw = [(e, "ical") for e in extract_ical(body)]
    elif "json" in content_type or body.lstrip().startswith(("{", "[")):
        try:
            raw = [(e, "jsonld") for e in event_objects(json.loads(body))]
        except (ValueError, RecursionError):
            raw, failures = [], 1
    else:
        if "<" not in body[:1024]:
            return [], [], [], 0, []
        structured, failures = extract_events(body)
        parser = CalendarLinks()
        parser.feed(body)
        raw = [(e, "jsonld") for e in structured] + [
            (e, "microdata") for e in parser.microdata
        ]
        visible_text = " ".join(parser.visible)
        raw.extend(
            (item, "organizer_schedule")
            for item in singapore_festival_schedule(url, visible_text)
        )
        raw.extend(
            (item, "organizer_schedule")
            for item in organizer_faq_schedule(url, visible_text)
        )
        links, external = discovery_links(
            parser, url, source.get("sourceType") == "discovery"
        )
        if not raw:
            hints = date_evidence(visible_text)
    return (
        [normalize_event(e, source, url, checked_at, kind) for e, kind in raw],
        links,
        hints,
        failures,
        external,
    )


def plain(value, limit=1500):
    if isinstance(value, dict):
        value = value.get("name", "")
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    ).strip()[:limit]


def iso_date(value):
    """Normalize explicit, unambiguous dates; never infer a year or swap month and day."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    numeric = re.fullmatch(
        r"(\d{4})([-/])(\d{1,2})\2(\d{1,2})(?:(?:T|\s+)\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
        value,
    )
    try:
        if numeric:
            return dt.date(
                int(numeric[1]), int(numeric[3]), int(numeric[4])
            ).isoformat()
        # Some organizer CMSs serialize a full English UTC timestamp.
        return (
            dt.datetime.strptime(value, "%a %b %d %H:%M:%S UTC %Y").date().isoformat()
        )
    except ValueError:
        return ""


def normalize_event(event, source, fetched_url, checked_at, kind="jsonld"):
    location = event.get("location", {})
    if isinstance(location, list):
        location = location[0] if location else {}
    location = location if isinstance(location, dict) else {"name": location}
    address = location.get("address", {})
    address = address if isinstance(address, dict) else {}
    record = {
        "name": plain(event.get("name")),
        "nameEn": "",
        "region": source["region"],
        "country": plain(address.get("addressCountry")),
        "city": plain(address.get("addressLocality")),
        "venue": plain(location.get("name")),
        "startDate": iso_date(event.get("startDate")),
        "endDate": iso_date(event.get("endDate")),
        "topics": source["topics"],
        "description": plain(event.get("description")),
        "participants": "",
        "sourceId": source["id"],
        "sourceRootUrl": source["url"],
        "sourceUrl": fetched_url,
        "sourceType": source.get("sourceType", "event"),
        "extractionMethod": kind,
        "eventUrl": same_origin_url(fetched_url, event.get("url")),
        "sourceNote": plain(event.get("sourcePassage"))
        or "官网结构化活动数据（" + kind + "）。",
        "verification": "pending",
        "eventStatus": plain(event.get("eventStatus")),
    }
    record["missingFields"] = [
        field
        for field in ("name", "startDate", "endDate", "city", "country")
        if not record[field]
    ]
    if (
        record["startDate"]
        and record["endDate"]
        and record["endDate"] < record["startDate"]
    ):
        record["missingFields"].append("invalidDateRange")
    if event.get("superEvent"):
        record["missingFields"].append("partOfLargerEvent")
    if event.get("recurrence"):
        record["missingFields"].append("recurrenceNeedsReview")
    evidence = {
        key: event[key]
        for key in (
            "@id",
            "name",
            "startDate",
            "endDate",
            "location",
            "url",
            "eventStatus",
            "sourcePassage",
            "superEvent",
        )
        if key in event
    }
    record["evidence"] = evidence
    content_hash = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    record.update(
        {
            "id": "candidate-" + content_hash[:16],
            "contentHash": content_hash,
            "collectedAt": checked_at,
            "reviewStatus": "pending",
        }
    )
    return record


def unique_candidates(candidates):
    result = {}
    for item in candidates:
        digest = item["contentHash"]
        existing = result.get(digest)
        if existing is None:
            result[digest] = item.copy()
        elif item.get("collectedAt", "") > existing.get("collectedAt", ""):
            # The hash includes origin, page URL and raw evidence; keep prior review decisions.
            existing["collectedAt"] = item["collectedAt"]
    return list(result.values())


def robots_permission(fetcher, source_url):
    parsed = urlsplit(source_url)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    result = fetcher.get(robots_url)
    if result["status"] in {404, 410}:
        return True, 2, "robots.txt not present (404/410)", None
    if result["status"] != 200:
        return False, 2, "robots.txt unavailable: HTTP " + str(result["status"]), None
    parser = RobotFileParser(robots_url)
    parser.parse(result["body"].splitlines())
    delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 2
    rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)
    return (
        parser.can_fetch(USER_AGENT, source_url),
        max(delay, 2),
        "robots.txt checked",
        parser,
    )


def collect_source(source, fetcher, checked_at):
    status = {
        "id": source["id"],
        "name": source["name"],
        "url": source["url"],
        "checkedAt": checked_at,
        "candidateCount": 0,
    }
    try:
        allowed, delay, note, rules = robots_permission(fetcher, source["url"])
        status["robots"] = note
        if not allowed:
            status.update(
                {
                    "status": "blocked",
                    "message": "robots.txt 禁止访问或无法可靠读取，未抓取页面。",
                }
            )
            return status, []
        if delay > 60:
            status.update(
                {
                    "status": "deferred",
                    "message": "官网要求较长访问间隔，需单独排期，未抓取页面。",
                }
            )
            return status, []
        result = fetcher.get(source["url"], delay=delay, robots=rules)
        status["httpStatus"] = result["status"]
        status["resolvedUrl"] = result["url"]
        if result["status"] != 200:
            status.update(
                {"status": "error", "message": "页面返回 HTTP " + str(result["status"])}
            )
            return status, []
        records, links, hints, failures, external = page_records(
            result, source, checked_at
        )
        page_log = [
            {
                "url": result["url"],
                "status": "ok",
                "candidates": len(records),
                "dateMentions": hints,
                "externalLinksForReview": external,
            }
        ]
        visited = {source["url"], result["url"]}
        # One calendar/discovery hop, then event detail pages, at most four extra GETs per source.
        budget = source.get("maxLinkedPages", 4)
        queue = [(url, 1) for url in rotate_links(links, budget, checked_at)]
        requests = 0
        while queue and requests < budget:
            child, depth = queue.pop(0)
            if child in visited:
                continue
            visited.add(child)
            if rules is not None and not rules.can_fetch(USER_AGENT, child):
                page_log.append({"url": child, "status": "blocked"})
                continue
            requests += 1
            try:
                extra = fetcher.get(child, delay=delay, robots=rules)
                visited.add(extra["url"])
                if extra["status"] != 200:
                    page_log.append(
                        {"url": child, "status": "http_" + str(extra["status"])}
                    )
                    continue
                found, next_links, dates, n, external = page_records(
                    extra, source, checked_at
                )
                records.extend(found)
                failures += n
                page_log.append(
                    {
                        "url": extra["url"],
                        "status": "ok",
                        "candidates": len(found),
                        "dateMentions": dates,
                        "externalLinksForReview": external,
                    }
                )
                if depth < 2:
                    next_links = [url for url in next_links if url not in visited]
                    next_links = rotate_links(next_links, budget, checked_at)
                    # Give one detail page a turn even on long organizer indexes.
                    if next_links and next_links[0] not in {url for url, _ in queue}:
                        queue.insert(0, (next_links[0], depth + 1))
                    queue.extend((url, depth + 1) for url in next_links)
            except (
                OSError,
                ValueError,
                http.client.HTTPException,
                RecursionError,
            ) as exc:
                page_log.append(
                    {"url": child, "status": "error", "message": plain(str(exc), 300)}
                )
        candidates = unique_candidates(records)
        status.update(
            {
                "candidateCount": len(candidates),
                "jsonLdParseErrors": failures,
                "pages": page_log,
                "remainingLinks": len(
                    {url for url, depth in queue if url not in visited}
                ),
                "status": "success" if candidates else "manual_review",
                "message": "已提取官网结构化候选。"
                if candidates
                else "未提取到结构化活动，保留日期线索供核对。",
            }
        )
        return status, candidates
    except (
        OSError,
        ValueError,
        http.client.HTTPException,
        ssl.SSLError,
        RecursionError,
    ) as exc:
        status.update({"status": "error", "message": plain(str(exc), 300)})
        return status, []


def load_config(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    seen = set()
    for source in sources:
        for field in ("id", "name", "url", "region", "topics"):
            if field not in source:
                raise ValueError("Source is missing " + field)
        if source["id"] in seen:
            raise ValueError("Duplicate source id: " + source["id"])
        seen.add(source["id"])
        source["url"] = safe_url(source["url"])
        if (
            not isinstance(source.get("maxLinkedPages", 4), int)
            or not 0 <= source.get("maxLinkedPages", 4) <= 20
        ):
            raise ValueError("maxLinkedPages must be in [0,20]")
        if source.get("official") is not True:
            raise ValueError("Each source must be explicitly marked official: true")
        if (
            (
                source["region"] not in REGIONS
                and not (
                    source.get("sourceType") == "discovery" and source["region"] == ""
                )
            )
            or not isinstance(source["topics"], list)
            or not set(source["topics"]) <= TOPICS
        ):
            raise ValueError("Unrecognized region/topic in " + source["id"])
    return [source for source in sources if source.get("enabled", True)]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "data/sources.json")
    parser.add_argument("--output-dir", type=Path, default=root / "data")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration only; no network access or output writes",
    )
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel official origins; requests within each origin remain sequential",
    )
    args = parser.parse_args()
    try:
        sources = load_config(args.config)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "configuration_valid",
                    "enabledSources": len(sources),
                    "networkRequests": 0,
                    "filesWritten": 0,
                },
                ensure_ascii=False,
            )
        )
        return 0
    checked_at = timestamp()
    groups = defaultdict(list)
    for source in sources:
        groups[origin(source["url"])].append(source)

    def scan_origin(group):
        fetcher = Fetcher(timeout=max(3, min(args.timeout, 60)))
        group_results = []
        for source in group:
            status, found = collect_source(source, fetcher, checked_at)
            print(source["id"] + ": " + status["status"], flush=True)
            group_results.append((status, found))
        return group_results

    statuses, candidates = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as pool:
        for result in pool.map(scan_origin, groups.values()):
            for status, found in result:
                statuses.append(status)
                candidates.extend(found)
    queue_path = args.output_dir / "review-queue.json"
    previous = (
        json.loads(queue_path.read_text(encoding="utf-8")).get("candidates", [])
        if queue_path.exists()
        else []
    )
    merged = unique_candidates(previous + candidates)
    new_count = len(merged) - len(unique_candidates(previous))
    atomic_json(
        queue_path,
        {
            "updatedAt": checked_at,
            "notice": "自动采集候选，未经人工审核，不等同于已核验展会。",
            "candidates": merged,
        },
    )
    status_path = args.output_dir / "collection-status.json"
    status = {
        "mode": "source_monitoring",
        "state": "completed" if sources else "not_configured",
        "lastRunAt": checked_at,
        "scheduled": False,
        "scheduleNote": "此文件仅证明一次脚本执行；定时任务是否启用须在 GitHub Actions 核实。",
        "sourceCount": len(sources),
        "newCandidateCount": new_count,
        "pendingCount": sum(
            item.get("reviewStatus") == "pending" for item in merged
        ),
        "sources": statuses,
    }
    atomic_json(status_path, status)
    # Role rosters use the same edition time and origin/robots rules.
    import company_capture
    company_capture.scan(args.config.resolve().parent, args.output_dir, checked_at,
                         timeout=max(3, min(args.timeout, 60)), workers=args.workers)
    status["completedAt"] = timestamp()
    atomic_json(status_path, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
