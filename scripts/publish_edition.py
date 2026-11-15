"""Build one Architecture Underlined edition and retain its dated snapshot."""

import json
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
SOURCES = {
    "ArchDaily": ("https://www.archdaily.com/rss", "archdaily.com"),
    "Dezeen": ("https://www.dezeen.com/feed/", "dezeen.com"),
    "The Architect’s Newspaper": ("https://www.archpaper.com/feed/", "archpaper.com"),
    "designboom": ("https://www.designboom.com/feed/", "designboom.com"),
    "Metropolis": ("https://metropolismag.com/feed/", "metropolismag.com"),
    "Archinect": ("https://archinect.com/news/rss", "archinect.com"),
    "Wallpaper": ("https://www.wallpaper.com/feed", "wallpaper.com"),
}
EDITION = re.compile(r'(<script type="application/json" id="edition">)(.*?)(</script>)', re.S)
ISSUE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, text):
        self.parts.append(text)


def plain_text(value):
    parser = PlainText()
    parser.feed(unescape(value))
    return " ".join(" ".join(parser.parts).split())


def publisher_excerpt(value):
    text = plain_text(value)
    text = re.split(r"\bRead more\b|\bThe post .+? appeared first on\b", text, maxsplit=1)[0]
    # Use a brief excerpt and credit the publisher in the page.
    words = text.split()
    return " ".join(words[:24]).rstrip(" ,;:") + ("…" if len(words) > 24 else "")


def field(entry, *names):
    for name in names:
        for child in entry:
            if child.tag.rsplit("}", 1)[-1] == name:
                if name == "link":
                    if child.get("rel", "alternate") == "alternate":
                        return (child.get("href") or child.text or "").strip()
                else:
                    return "".join(child.itertext()).strip()
    return ""


def feed_date(value):
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date is None or date.tzinfo is None:
        raise ValueError("Feed date has no time zone")
    return date.astimezone(NEW_YORK)


def source_url(value, domain):
    url = urlsplit(value)
    host = url.hostname or ""
    if url.scheme != "https" or url.username or not (host == domain or host.endswith("." + domain)):
        return ""
    query = urlencode([
        (key, value) for key, value in parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ])
    return urlunsplit(url._replace(fragment="", query=query))


def parse_feed(raw, name, domain, since, cutoff):
    entries = ET.fromstring(raw).iter()
    for entry in entries:
        if entry.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        try:
            stamp = feed_date(field(entry, "pubDate", "published", "date", "updated"))
            url = source_url(field(entry, "link"), domain)
        except (TypeError, ValueError):
            continue
        title = plain_text(field(entry, "title"))
        if since < stamp <= cutoff and url and title:
            yield stamp, {
                "title": title, "source": name, "url": url, "date": stamp.date().isoformat(),
                "summary": publisher_excerpt(field(entry, "description", "summary")),
            }


def collect(since, cutoff, previous):
    def read(source):
        name, (url, domain) = source
        try:
            request = Request(url, headers={"User-Agent": "Architecture Underlined RSS reader/1.0"})
            with urlopen(request, timeout=12) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("Feed exceeds size limit")
            return list(parse_feed(raw, name, domain, since, cutoff))
        except (OSError, ValueError, ET.ParseError) as error:
            print(f"{name}: {error}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        results = [result for result in pool.map(read, SOURCES.items()) if result is not None]
    if not results:
        raise RuntimeError("All configured news feeds failed. Preserving the current edition.")

    seen_urls = {story["url"] for story in previous}
    seen_titles = set()
    per_source = {}
    selected = []
    entries = sorted((entry for result in results for entry in result), key=lambda entry: entry[0], reverse=True)
    for _, story in entries:
        key = re.sub(r"\W+", "", story["title"].casefold())
        source = story["source"]
        if story["url"] in seen_urls or key in seen_titles or per_source.get(source, 0) >= 4:
            continue
        seen_urls.add(story["url"])
        seen_titles.add(key)
        per_source[source] = per_source.get(source, 0) + 1
        selected.append(story)
        if len(selected) == 20:
            break
    return selected


def render(page, edition):
    payload = json.dumps(edition, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    title = "Architecture Underlined · " + edition["date"]
    page = re.sub(r"<title>[^<]*</title>", f"<title>{title}</title>", page, count=1)
    masthead = f'<span class="flag">{edition["date"]}</span>'
    page = re.sub(r'<span class="flag">[^<]*</span>', masthead, page, count=1)
    return EDITION.sub(lambda match: match[1] + payload + match[3], page, count=1)


def publish(root, now, load_stories=collect):
    page_path = root / "index.html"
    page = page_path.read_text(encoding="utf-8")
    match = EDITION.search(page)
    if match is None:
        raise RuntimeError("Missing edition data in index.html")
    previous = json.loads(match[2])

    archive = root / "archive"
    today = now.date().isoformat()
    snapshot = archive / today / "index.html"
    archived_edition = EDITION.search(snapshot.read_text(encoding="utf-8")) if snapshot.exists() else None
    if archived_edition:
        edition = json.loads(archived_edition[2])
        print(f"Edition already archived for {today}. Preserving its content.")
    elif snapshot.exists() and previous["date"] == today:
        edition = previous
    else:
        if snapshot.exists():
            print(f"Preserving the pre-redesign snapshot for {today}.")
        cutoff = min(now, now.replace(hour=7, minute=0, second=0, microsecond=0))
        since = datetime.fromisoformat(previous["cutoff"]) if previous.get("cutoff") else cutoff - timedelta(days=3)
        stories = load_stories(since, cutoff, previous["stories"])
        edition = dict(previous, date=today, cutoff=cutoff.isoformat())
        if stories:
            edition.update(stories=stories, news_date=today, kind="feed")
        else:
            print("No eligible new headlines. Carrying the previous news with its original edition date.")

    issue_dates = {
        path.parent.name for path in archive.glob("*/index.html") if ISSUE_DATE.fullmatch(path.parent.name)
    }
    if not snapshot.exists():
        issue_dates.add(today)
    edition["issues"] = sorted(issue_dates, reverse=True)
    output = render(page, edition)
    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(output, encoding="utf-8")
    page_path.write_text(output, encoding="utf-8")
    (archive / "issues.json").write_text(json.dumps(edition["issues"]) + "\n", encoding="utf-8")
    print(f"Homepage edition {edition['date']}: {len(edition['stories'])} stories.")


if __name__ == "__main__":
    publish(Path.cwd(), datetime.now(NEW_YORK))
