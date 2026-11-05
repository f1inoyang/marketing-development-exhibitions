#!/usr/bin/env python3
"""Collect Event JSON-LD from explicitly approved official sources for human review.

Python 3.9+, standard library only. Published events.json is never modified.
"""

import argparse
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

USER_AGENT = "ExhibitionAtlasBot/0.1"
MAX_BYTES = 2 * 1024 * 1024
EVENT_TYPES = {"Event", "BusinessEvent", "ExhibitionEvent", "EducationEvent", "ConferenceEvent"}
REGIONS = {"asia", "europe", "africa", "north-america", "south-america", "oceania"}
TOPICS = {"fintech", "banking", "telecom", "digital-currency", "packaging", "identity"}


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_url(url):
    """Allow only credential-free public HTTPS URLs on the default TLS port."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
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
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
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
                raise ValueError("Cross-origin redirect blocked; review and configure the canonical official URL")
            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                raise ValueError("Redirect destination is disallowed by robots.txt")
            parsed = urlsplit(url)
            wait = max(self.delay, delay or 0) - (time.monotonic() - self.last_request.get(initial_origin, 0))
            if wait > 0:
                time.sleep(wait)
            connection = PinnedHTTPSConnection(parsed.hostname, 443, timeout=self.timeout, context=ssl.create_default_context())
            self.last_request[initial_origin] = time.monotonic()
            try:
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain;q=0.8", "Accept-Encoding": "identity"})
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
                encoding = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
                charset = encoding.group(1) if encoding else "utf-8"
                try:
                    text = body.decode(charset, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return {"status": response.status, "headers": headers, "body": text, "url": url}
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
            kind = dict(attrs).get("type", "").split(";")[0].strip().lower()
            self.collect = kind == "application/ld+json"
            self.parts = []

    def handle_data(self, data):
        if self.collect:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.collect:
            self.blocks.append("".join(self.parts))
            self.collect = False


def event_objects(value):
    """Traverse @graph, ItemList, nested itemListElement and ordinary arrays."""
    if isinstance(value, list):
        for item in value:
            yield from event_objects(item)
    elif isinstance(value, dict):
        kinds = value.get("@type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        kinds = {str(kind).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] for kind in kinds}
        if kinds & EVENT_TYPES:
            yield value
        for key, item in value.items():
            if key != "@context" and isinstance(item, (dict, list)):
                yield from event_objects(item)


def extract_events(document):
    parser = JsonLDParser()
    parser.feed(document)
    events, failures = [], 0
    for block in parser.blocks:
        try:
            events.extend(event_objects(json.loads(block)))
        except (ValueError, TypeError, RecursionError):
            failures += 1
    return events, failures


def plain(value, limit=1500):
    if isinstance(value, dict):
        value = value.get("name", "")
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()[:limit]


def iso_date(value):
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T|\s)", value):
        return ""
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return ""


def normalize_event(event, source, fetched_url, checked_at):
    location = event.get("location", {})
    if isinstance(location, list):
        location = location[0] if location else {}
    location = location if isinstance(location, dict) else {"name": location}
    address = location.get("address", {})
    address = address if isinstance(address, dict) else {}
    record = {
        "name": plain(event.get("name")), "nameEn": "", "region": source["region"],
        "country": plain(address.get("addressCountry")), "city": plain(address.get("addressLocality")),
        "venue": plain(location.get("name")), "startDate": iso_date(event.get("startDate")),
        "endDate": iso_date(event.get("endDate")), "topics": source["topics"],
        "description": plain(event.get("description")), "participants": "",
        "sourceId": source["id"], "sourceUrl": fetched_url, "eventUrl": plain(event.get("url")),
        "sourceNote": "官网 JSON-LD 自动提取；日期、地点和届次均待人工复核。",
        "verification": "pending", "eventStatus": plain(event.get("eventStatus")),
    }
    record["missingFields"] = [field for field in ("name", "startDate", "endDate", "city", "country") if not record[field]]
    if record["startDate"] and record["endDate"] and record["endDate"] < record["startDate"]:
        record["missingFields"].append("invalidDateRange")
    evidence = {key: event[key] for key in ("@id", "name", "startDate", "endDate", "location", "url", "eventStatus") if key in event}
    record["evidence"] = evidence
    content_hash = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    record.update({"id": "candidate-" + content_hash[:16], "contentHash": content_hash, "collectedAt": checked_at, "reviewStatus": "pending"})
    return record


def unique_candidates(candidates):
    result = {}
    for item in candidates:
        result.setdefault(item["contentHash"], item)
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
    return parser.can_fetch(USER_AGENT, source_url), max(delay, 2), "robots.txt checked", parser


def collect_source(source, fetcher, checked_at):
    status = {"id": source["id"], "name": source["name"], "url": source["url"], "checkedAt": checked_at, "candidateCount": 0}
    try:
        allowed, delay, note, rules = robots_permission(fetcher, source["url"])
        status["robots"] = note
        if not allowed:
            status.update({"status": "blocked", "message": "robots.txt 禁止访问或无法可靠读取，未抓取页面。"})
            return status, []
        if delay > 60:
            status.update({"status": "deferred", "message": "官网要求较长访问间隔，需单独排期，未抓取页面。"})
            return status, []
        result = fetcher.get(source["url"], delay=delay, robots=rules)
        status["httpStatus"] = result["status"]
        status["resolvedUrl"] = result["url"]
        if result["status"] != 200:
            status.update({"status": "error", "message": "页面返回 HTTP " + str(result["status"])})
            return status, []
        events, failures = extract_events(result["body"])
        candidates = unique_candidates([normalize_event(event, source, result["url"], checked_at) for event in events])
        status["candidateCount"] = len(candidates)
        status["jsonLdParseErrors"] = failures
        status.update({"status": "success" if candidates else "manual_review", "message": "已提取候选，需人工审核后发布。" if candidates else "页面可访问，但无可解析的 Event JSON-LD；需人工核验或专用解析器。"})
        return status, candidates
    except (OSError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
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
        if source.get("official") is not True:
            raise ValueError("Each source must be explicitly marked official: true")
        if source["region"] not in REGIONS or not isinstance(source["topics"], list) or not set(source["topics"]) <= TOPICS:
            raise ValueError("Unrecognized region/topic in " + source["id"])
    return [source for source in sources if source.get("enabled", True)]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "data/sources.json")
    parser.add_argument("--output-dir", type=Path, default=root / "data")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration only; no network access or output writes")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    try:
        sources = load_config(args.config)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps({"status": "configuration_valid", "enabledSources": len(sources), "networkRequests": 0, "filesWritten": 0}, ensure_ascii=False))
        return 0
    checked_at = timestamp()
    fetcher = Fetcher(timeout=max(3, min(args.timeout, 60)))
    statuses, candidates = [], []
    for source in sources:
        status, found = collect_source(source, fetcher, checked_at)
        statuses.append(status)
        candidates.extend(found)
        print(source["id"] + ": " + status["status"], flush=True)
    queue_path = args.output_dir / "review-queue.json"
    previous = json.loads(queue_path.read_text(encoding="utf-8")).get("candidates", []) if queue_path.exists() else []
    merged = unique_candidates(previous + candidates)
    new_count = len(merged) - len(unique_candidates(previous))
    atomic_json(queue_path, {"updatedAt": checked_at, "notice": "自动采集候选，未经人工审核，不等同于已核验展会。", "candidates": merged})
    atomic_json(args.output_dir / "collection-status.json", {
        "mode": "source_monitoring", "state": "completed" if sources else "not_configured", "lastRunAt": checked_at,
        "scheduled": False, "scheduleNote": "此文件仅证明一次脚本执行；定时任务是否启用须在 GitHub Actions 核实。",
        "sourceCount": len(sources), "newCandidateCount": new_count,
        "pendingCount": sum(item.get("reviewStatus") == "pending" for item in merged), "sources": statuses,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
