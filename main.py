"""
HWBOT HWP Calculator

A small desktop GUI that reads a public HWBOT CPU/GPU hardware page and groups
the available hardware points by benchmark.

Install dependencies:
    pip install -r requirements.txt

Run:
    python main.py

Build a Windows exe:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "HWBOT HWP Calculator" main.py
"""

from __future__ import annotations

import re
import csv
import base64
import tkinter as tk
from tkinter import messagebox, filedialog
import threading
from collections import Counter
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import webbrowser
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import customtkinter as ctk
import requests
from bs4 import BeautifulSoup, Tag


APP_TITLE = "HWBOT HWP Calculator"
GITHUB_URL = "https://github.com/hxntxihero"
REQUEST_TIMEOUT_SECONDS = 25
MAX_DOWNLOAD_BYTES = 5_000_000
MAX_BENCHMARK_SCAN = 55
RANKING_PAGE_SIZE = 1000
MAX_COOLING_CHECKS = 35

HTML_CACHE: dict[tuple[str, bool], str] = {}
CACHE_LOCK = threading.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 HWBOT-HWP-Calculator/1.0"
)


class CalculatorError(Exception):
    """User-facing error raised when a page cannot be parsed safely."""


@dataclass(frozen=True)
class BenchmarkPoint:
    benchmark: str
    points: Decimal
    submission_url: str | None = None
    cooling: str | None = None


@dataclass(frozen=True)
class CalculationResult:
    title: str
    page_url: str
    benchmarks: tuple[BenchmarkPoint, ...]

    @property
    def grouped_points(self) -> Counter[Decimal]:
        return Counter(item.points for item in self.benchmarks)

    @property
    def total_potential(self) -> Decimal:
        return sum((item.points for item in self.benchmarks), Decimal("0"))


@dataclass(frozen=True)
class HardwareTarget:
    name: str
    key: str
    hardware_type: str
    core_count: str | None = None


COOLING_IDS = {
    "Air": "1",
    "Water": "2",
    "Dry Ice": "3",
    "Phase Change": "4",
    "Cascade": "5",
    "Liquid Nitrogen": "6",
    "Liquid Helium": "7",
}

def validate_hwbot_url(raw_url: str) -> str:
    """Return a normalized URL or raise a clear error for unsafe input."""
    url = raw_url.strip()
    if not url:
        raise CalculatorError("Paste a HWBOT hardware URL first.")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise CalculatorError("The URL must start with https:// or http://.")

    host = parsed.netloc.lower()
    if host not in {"hwbot.org", "www.hwbot.org"}:
        raise CalculatorError("Only public URLs from hwbot.org are supported.")

    supported_hardware_paths = (
        "/hardware/",
        "/hardware/processors",
        "/hardware/videocards",
        "/hardware/motherboards",
    )
    if not parsed.path.startswith(supported_hardware_paths):
        raise CalculatorError(
            "This does not look like a HWBOT hardware page. "
            "Use a URL such as https://hwbot.org/hardware/processors?key=..."
        )

    return url


def download_html(url: str, ajax: bool = False, use_cache: bool = True) -> str:
    """Download the page with conservative limits and helpful errors."""
    cache_key = (url, ajax)
    if use_cache:
        with CACHE_LOCK:
            cached = HTML_CACHE.get(cache_key)
        if cached is not None:
            return cached

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"

        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
        )
    except requests.Timeout as exc:
        raise CalculatorError("HWBOT did not respond in time. Try again in a moment.") from exc
    except requests.RequestException as exc:
        raise CalculatorError(f"There was a problem downloading data: {exc}") from exc

    if response.status_code in {403, 429, 503}:
        raise CalculatorError(
            "HWBOT or Cloudflare blocked the request. Open the page in a browser first, "
            "or try again later when the public page is available."
        )

    if not response.ok:
        raise CalculatorError(
            f"HWBOT returned HTTP {response.status_code}. Check the URL and try again."
        )

    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        raise CalculatorError("The URL did not return an HTML page.")

    chunks: list[bytes] = []
    total_size = 0
    try:
        for chunk in response.iter_content(chunk_size=64_000):
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > MAX_DOWNLOAD_BYTES:
                raise CalculatorError("The page is unexpectedly large, so it was not parsed.")
            chunks.append(chunk)
    finally:
        response.close()

    encoding = response.encoding or "utf-8"
    html = b"".join(chunks).decode(encoding, errors="replace")
    if use_cache:
        with CACHE_LOCK:
            if len(HTML_CACHE) > 250:
                HTML_CACHE.clear()
            HTML_CACHE[cache_key] = html
    return html


def page_looks_like_cloudflare(html: str) -> bool:
    lowered = html.lower()
    signals = (
        "checking your browser",
        "cloudflare",
        "cf-browser-verification",
        "cf-challenge",
        "turnstile",
    )
    return any(signal in lowered for signal in signals)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None
    return number.normalize()


def parse_optional_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    number = parse_decimal(value)
    if number is None:
        raise CalculatorError(f"'{value}' is not a valid HWP number.")
    return number


def format_points(points: Decimal) -> str:
    if points == points.to_integral():
        return str(int(points))
    return f"{points:f}".rstrip("0").rstrip(".")


def find_page_title(soup: BeautifulSoup, fallback_url: str) -> str:
    heading = soup.find(["h1", "h2"])
    if heading:
        text = clean_text(heading.get_text(" "))
        if text:
            return text

    if soup.title and soup.title.string:
        text = clean_text(soup.title.string)
        if text:
            return text.replace(" - HWBOT", "")

    return fallback_url


def row_has_hardware_points_context(row_text: str) -> bool:
    lowered = row_text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "hardware points",
            "hardware points:",
            "hwp",
            "max points",
            "maximum points",
            "top points",
        )
    )


def extract_points_from_text(text: str) -> Decimal | None:
    """Extract a likely HWP value from visible text."""
    patterns = (
        r"(?:maximum|max|top)\s+points?\s*[:\-]?\s*(\d+(?:[.,]\d+)?)",
        r"(\d+(?:[.,]\d+)?)\s*(?:hardware\s*)?points?\b",
        r"(\d+(?:[.,]\d+)?)\s*hwp\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_decimal(match.group(1))
    return None


def header_map_for_table(table: Tag) -> dict[str, int]:
    headers = [clean_text(th.get_text(" ")).lower() for th in table.find_all("th")]
    return {header: index for index, header in enumerate(headers)}


def detect_points_column(headers: dict[str, int]) -> int | None:
    keywords = ("maximum points", "max points", "top points", "hardware points", "points", "hwp")
    for header, index in headers.items():
        if any(keyword in header for keyword in keywords):
            return index
    return None


def detect_benchmark_column(headers: dict[str, int]) -> int:
    keywords = ("benchmark", "category", "application", "test")
    for header, index in headers.items():
        if any(keyword in header for keyword in keywords):
            return index
    return 0


def parse_table_rows(soup: BeautifulSoup) -> list[BenchmarkPoint]:
    """Parse benchmark rows from HTML tables when HWBOT exposes a table."""
    points: list[BenchmarkPoint] = []
    seen: set[tuple[str, Decimal]] = set()

    for table in soup.find_all("table"):
        headers = header_map_for_table(table)
        point_column = detect_points_column(headers)
        benchmark_column = detect_benchmark_column(headers)

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            cell_texts = [clean_text(cell.get_text(" ")) for cell in cells]
            row_text = clean_text(" ".join(cell_texts))

            row_points: Decimal | None = None
            if point_column is not None and point_column < len(cell_texts):
                row_points = extract_points_from_text(cell_texts[point_column])
                if row_points is None:
                    row_points = parse_decimal(
                        re.sub(r"[^\d,.]", "", cell_texts[point_column]).strip(".,")
                    )

            if row_points is None and row_has_hardware_points_context(row_text):
                row_points = extract_points_from_text(row_text)

            if row_points is None or row_points <= 0:
                continue

            benchmark = (
                cell_texts[benchmark_column]
                if benchmark_column < len(cell_texts)
                else cell_texts[0]
            )
            benchmark = clean_benchmark_name(benchmark, row_points)
            key = (benchmark.lower(), row_points)
            if key not in seen:
                points.append(BenchmarkPoint(benchmark=benchmark, points=row_points))
                seen.add(key)

    return points


def clean_benchmark_name(name: str, points: Decimal) -> str:
    name = clean_text(name)
    name = re.sub(r"\b(?:maximum|max|top)?\s*points?\b.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(rf"\b{re.escape(format_points(points))}\s*(?:hwp|points?)\b", "", name, flags=re.IGNORECASE)
    name = clean_text(name.strip(":-|"))
    return name or "Unnamed benchmark"


def parse_card_like_blocks(soup: BeautifulSoup) -> list[BenchmarkPoint]:
    """
    Fallback parser for responsive layouts where rows are div/list/card blocks
    instead of classic tables.
    """
    candidates: list[BenchmarkPoint] = []
    seen: set[tuple[str, Decimal]] = set()
    selector = (
        "li, article, section, div[class*='benchmark'], div[class*='table'], "
        "div[class*='row'], div[class*='card']"
    )

    for block in soup.select(selector):
        text = clean_text(block.get_text(" "))
        if not text or not row_has_hardware_points_context(text):
            continue

        points = extract_points_from_text(text)
        if points is None or points <= 0:
            continue

        heading = block.find(["h2", "h3", "h4", "a", "strong"])
        name = clean_text(heading.get_text(" ")) if heading else text[:90]
        name = clean_benchmark_name(name, points)

        key = (name.lower(), points)
        if key not in seen:
            candidates.append(BenchmarkPoint(benchmark=name, points=points))
            seen.add(key)

    return candidates


def parse_hwbot_page(html: str, url: str) -> CalculationResult:
    """Convert HWBOT HTML into grouped hardware point information."""
    if page_looks_like_cloudflare(html):
        raise CalculatorError(
            "There was a problem downloading data: Cloudflare challenge page was returned. "
            "No points were found because the real HWBOT page was not returned."
        )

    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup(["script", "style", "noscript", "svg"]):
        unwanted.decompose()

    table_points = parse_table_rows(soup)
    fallback_points = parse_card_like_blocks(soup)

    benchmarks = tuple(table_points or fallback_points)
    if not benchmarks:
        raise CalculatorError(
            "No hardware point values were found on this page. "
            "Some HWBOT hardware overview pages only show specifications, not HWP tables. "
            "If HWBOT changed the page layout, the parser may need an update."
        )

    return CalculationResult(
        title=find_page_title(soup, url),
        page_url=url,
        benchmarks=benchmarks,
    )


def parse_hardware_target(html: str, url: str) -> HardwareTarget | None:
    """Read the hardware key/name from a HWBOT hardware overview URL."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    key_values = query.get("key")
    if not key_values:
        return None

    if parsed.path.startswith("/hardware/processors") or "/processor/" in parsed.path:
        hardware_type = "cpu"
    elif parsed.path.startswith("/hardware/videocards") or "/videocard/" in parsed.path:
        hardware_type = "gpu"
    elif parsed.path.startswith("/hardware/motherboards") or "/motherboard/" in parsed.path:
        hardware_type = "motherboard"
    else:
        return None

    soup = BeautifulSoup(html, "html.parser")
    meta_title = soup.find("meta", attrs={"property": "og:title"})
    if meta_title and meta_title.get("content"):
        name = clean_text(meta_title["content"])
    elif soup.title and soup.title.string:
        name = clean_text(soup.title.string)
    else:
        name = find_page_title(soup, url)
    name = re.sub(r"\s+(?:Specs|Features)\b.*$", "", name, flags=re.IGNORECASE).strip()

    return HardwareTarget(
        name=name or key_values[0],
        key=key_values[0],
        hardware_type=hardware_type,
        core_count=None,
    )


def build_hardware_ajax_url(target: HardwareTarget, source_url: str) -> str | None:
    """Build the HWBOT AJAX URL that contains the selected hardware records."""
    parsed = urlparse(source_url)
    path = parsed.path
    parts = target.key.split("-")

    if target.hardware_type == "cpu":
        if path.startswith("/hardware/processors") and len(parts) >= 3:
            manufacturer, selector = parts[0], parts[1]
            if selector == "architecture":
                if len(parts) < 5:
                    return None
                family, subfamily, cpu = parts[2], parts[3], parts[4]
                return (
                    "https://hwbot.org/hardware/manufacturers/"
                    f"{manufacturer}/cpuFamilies/{family}/cpuSubFamilies/{subfamily}/cpus/{cpu}"
                )
            if selector == "socket" and len(parts) >= 4:
                socket, cpu = parts[2], parts[3]
                return (
                    "https://hwbot.org/hardware/manufacturers/"
                    f"{manufacturer}/sockets/{socket}/cpus/{cpu}"
                )

        # Old processor URLs redirect to the new keyed page. If the key is not
        # available, the direct fragment cannot be built safely.

    if target.hardware_type == "gpu":
        if path.startswith("/hardware/videocards") and len(parts) >= 3:
            manufacturer, family, gpu = parts[0], parts[1], parts[2]
            return (
                "https://hwbot.org/hardware/manufacturers/"
                f"{manufacturer}/gpuFamilies/{family}/gpus/{gpu}"
            )

    if target.hardware_type == "motherboard":
        if path.startswith("/hardware/motherboards") and len(parts) >= 4:
            manufacturer, socket, chipset, motherboard = parts[0], parts[1], parts[2], parts[3]
            return (
                "https://hwbot.org/hardware/manufacturers/"
                f"{manufacturer}/sockets/{socket}/chipsets/{chipset}/mbs/{motherboard}"
            )

    return None


def parse_hardware_records_fragment(
    html: str,
    url: str,
    target: HardwareTarget | None = None,
) -> CalculationResult:
    """Parse HWBOT's AJAX hardware-record table."""
    if page_looks_like_cloudflare(html):
        raise CalculatorError(
            "Cloudflare returned a challenge page while loading the hardware record table."
        )

    soup = BeautifulSoup(html, "html.parser")
    title = find_page_title(soup, url)
    benchmarks: list[BenchmarkPoint] = []
    seen: set[str] = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [clean_text(cell.get_text(" ")).lower() for cell in rows[0].find_all(["td", "th"])]
        if "benchmark" not in headers or not any("hardware record" in header for header in headers):
            continue

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            benchmark = clean_text(cells[0].get_text(" "))
            record_text = clean_text(cells[1].get_text(" "))
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*HWP\b", record_text, flags=re.IGNORECASE)
            if not benchmark or not match:
                continue

            points = parse_decimal(match.group(1))
            if points is None or points <= 0:
                continue

            submission_url = None
            submission_link = row.find("a", href=re.compile(r"/submissions?/\d+"))
            if submission_link and submission_link.get("href"):
                submission_url = urljoin("https://hwbot.org", submission_link["href"])

            key = benchmark.lower()
            if key in seen:
                continue

            benchmarks.append(
                BenchmarkPoint(
                    benchmark=benchmark,
                    points=points,
                    submission_url=submission_url,
                )
            )
            seen.add(key)

    if not benchmarks:
        raise CalculatorError(
            "The HWBOT hardware record table loaded, but no positive HWP values were found."
        )

    return CalculationResult(title=title, page_url=url, benchmarks=tuple(benchmarks))


def discover_benchmark_links(hardware_type: str) -> list[tuple[str, str]]:
    """Return benchmark names and ranking URLs discovered from HWBOT's public overview."""
    html = download_html("https://hwbot.org/benchmarks")
    soup = BeautifulSoup(html, "html.parser")

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    gpu_section_started = False

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("/benchmarks/"):
            continue
        if "/rules/" in href or "/rankings" in href or "/submissions" in href:
            continue

        text = clean_text(anchor.get_text(" "))
        if not text:
            continue

        # On HWBOT's benchmark overview CPU benchmarks currently appear before
        # the classic GPU/3D benchmarks. This keeps CPU scans reasonably quick.
        if text.startswith("3DMark - Cloud Gate"):
            gpu_section_started = True
        if hardware_type == "cpu" and gpu_section_started and "(CPU" not in text:
            break

        parsed = urlparse(href)
        slug = parsed.path.rstrip("/").split("/")[-1]
        if not slug or slug in seen:
            continue

        query = parse_qs(parsed.query)
        ranking_query: dict[str, str] = {
            "hardwareType": hardware_type,
            "pageSize": str(RANKING_PAGE_SIZE),
        }
        if query.get("applicationVersionId"):
            ranking_query["applicationVersionId"] = query["applicationVersionId"][0]

        ranking_path = f"/benchmarks/{slug}/rankings"
        ranking_url = urlunparse(
            ("https", "hwbot.org", ranking_path, "", urlencode(ranking_query), "")
        )

        links.append((text, ranking_url))
        seen.add(slug)

        if len(links) >= MAX_BENCHMARK_SCAN:
            break

    return links


def row_matches_hardware(row: Tag, target: HardwareTarget) -> bool:
    for anchor in row.find_all("a", href=True):
        href = anchor["href"]
        if target.key in href:
            return True

    row_text = clean_text(row.get_text(" ")).lower()
    return target.name.lower() in row_text


def extract_hwp_from_ranking_table(
    html: str, 
    target: HardwareTarget, 
    benchmark_name: str,
    cooling_filter: str = "All cooling"
) -> BenchmarkPoint | None:
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_cells = [clean_text(cell.get_text(" ")) for cell in rows[0].find_all(["td", "th"])]
        header_text = " ".join(header_cells).lower()
        # Ignore summary tables that show global/hardware records regardless of filters
        if "hardware record" in header_text or "world record" in header_text:
            continue

        hwp_index = next(
            (index for index, text in enumerate(header_cells) if text.strip().lower() == "hwp"),
            None,
        )
        cooling_index = next(
            (index for index, text in enumerate(header_cells) if "cooling" in text.lower()),
            None,
        )

        if hwp_index is None:
            continue

        best_entry: BenchmarkPoint | None = None
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= hwp_index or not row_matches_hardware(row, target):
                continue

            # Secondary verification: Ensure the cooling in this specific row matches the filter
            if cooling_filter != "All cooling" and cooling_index is not None and cooling_index < len(cells):
                actual_cooling = clean_text(cells[cooling_index].get_text(" "))
                if not cooling_matches(actual_cooling, cooling_filter):
                    continue

            points = parse_decimal(clean_text(cells[hwp_index].get_text(" ")))
            if points is not None and points > 0:
                if best_entry is None or points > best_entry.points:
                    sub_link = row.find("a", href=re.compile(r"/submissions?/\d+"))
                    sub_url = urljoin("https://hwbot.org", sub_link["href"]) if sub_link else None
                    
                    actual_cooling_text = cooling_filter if cooling_filter != "All cooling" else None
                    if cooling_index is not None and cooling_index < len(cells):
                        actual_cooling_text = clean_text(cells[cooling_index].get_text(" "))

                    best_entry = BenchmarkPoint(
                        benchmark=benchmark_name, 
                        points=points, 
                        submission_url=sub_url,
                        cooling=actual_cooling_text
                    )

        if best_entry is not None:
            return best_entry

    return None


def scan_public_rankings_for_hardware(
    target: HardwareTarget,
    cooling_id: str | None = None,
    cooling_name: str = "All cooling",
    progress: Callable[[str], None] | None = None,
) -> tuple[BenchmarkPoint, ...]:
    """
    Fallback for HWBOT hardware overview pages.
    """
    benchmark_links = discover_benchmark_links(target.hardware_type)
    points: list[BenchmarkPoint] = []
    points_lock = threading.Lock()

    if progress:
        progress(f"Checking {len(benchmark_links)} benchmark rankings...")

    def fetch_ranking(item: tuple[str, str]) -> None:
        benchmark_name, ranking_url = item
        url = ranking_url
        if cooling_id:
            separator = "&" if urlparse(url).query else "?"
            url = f"{url}{separator}coolingTypeId={cooling_id}"
        if target.hardware_type == "cpu" and target.core_count:
            separator = "&" if urlparse(url).query else "?"
            url = f"{url}{separator}cores={target.core_count}"

        try:
            html = download_html(url)
            point_entry = extract_hwp_from_ranking_table(html, target, benchmark_name, cooling_name)
            if point_entry is not None:
                with points_lock:
                    points.append(point_entry)
        except CalculatorError:
            pass

    with ThreadPoolExecutor(max_workers=10) as executor:
        # Wrapping in list() ensures exceptions are raised and caught by our UI error handler
        list(executor.map(fetch_ranking, benchmark_links))

    return tuple(points)


def cooling_matches(actual: str | None, selected: str) -> bool:
    if selected == "All cooling":
        return True
    if actual is None:
        return False

    actual_lower = actual.lower()
    selected_lower = selected.lower()

    if selected_lower == "air":
        return "air" in actual_lower
    if selected_lower == "water":
        return "water" in actual_lower or "aio" in actual_lower or "h2o" in actual_lower
    if selected_lower == "phase change":
        return "phase" in actual_lower
    if selected_lower == "cascade":
        return "cascade" in actual_lower
    if selected_lower == "dry ice":
        return "dry ice" in actual_lower or "dice" in actual_lower
    if selected_lower == "liquid nitrogen":
        return "liquid nitrogen" in actual_lower or "ln2" in actual_lower
    if selected_lower == "liquid helium":
        return "liquid helium" in actual_lower or "lhe" in actual_lower

    return selected_lower in actual_lower


def extract_cooling_from_submission(html: str, target: HardwareTarget) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" "))

    labels = {
        "cpu": ("Processor Model", "Videocard Model", "Motherboard Model"),
        "gpu": ("Videocard Model", "Processor Model", "Motherboard Model"),
        "motherboard": ("Motherboard Model", "Processor Model", "Videocard Model"),
    }
    primary_label, *next_labels = labels.get(target.hardware_type, labels["cpu"])

    start = text.find(primary_label)
    if start == -1:
        return None

    end_candidates = [text.find(label, start + len(primary_label)) for label in next_labels]
    end_candidates = [candidate for candidate in end_candidates if candidate != -1]
    end = min(end_candidates) if end_candidates else min(len(text), start + 600)
    segment = text[start:end]

    match = re.search(
        r"\bCooling\s+(.+?)(?:\s+(?:Cores|Speed|Chipset|Memory|GPU|CPU|Bus|Socket|Model)\b|$)",
        segment,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    return clean_text(match.group(1))


def apply_cooling_filter(
    benchmarks: tuple[BenchmarkPoint, ...],
    target: HardwareTarget,
    selected_cooling: str,
    progress: Callable[[str], None] | None = None,
) -> tuple[BenchmarkPoint, ...]:
    if selected_cooling == "All cooling":
        return benchmarks

    checkable = [benchmark for benchmark in benchmarks if benchmark.submission_url]
    if len(checkable) > MAX_COOLING_CHECKS:
        if progress is not None:
            progress(
                f"Cooling filter limited to first {MAX_COOLING_CHECKS} matching records "
                "to avoid HWBOT rate limits"
            )
        checkable = checkable[:MAX_COOLING_CHECKS]

    filtered: list[BenchmarkPoint] = []
    total = len(checkable)
    for index, benchmark in enumerate(checkable, start=1):
        if progress is not None:
            progress(f"Checking cooling {index}/{total}: {benchmark.benchmark}")
        try:
            html = download_html(benchmark.submission_url)
        except CalculatorError as exc:
            if progress is not None:
                progress(f"Skipped cooling check: {exc}")
            continue
        cooling = extract_cooling_from_submission(html, target)
        if cooling_matches(cooling, selected_cooling):
            filtered.append(
                BenchmarkPoint(
                    benchmark=benchmark.benchmark,
                    points=benchmark.points,
                    submission_url=benchmark.submission_url,
                    cooling=cooling,
                )
            )

    if progress is not None:
        progress(f"Cooling filter kept {len(filtered)} of {total} checked records")
    return tuple(filtered)


def apply_hwp_range_filter(
    benchmarks: tuple[BenchmarkPoint, ...],
    min_hwp: Decimal | None,
    max_hwp: Decimal | None,
) -> tuple[BenchmarkPoint, ...]:
    if min_hwp is not None and max_hwp is not None and min_hwp > max_hwp:
        raise CalculatorError("Minimum HWP cannot be higher than maximum HWP.")

    return tuple(
        benchmark
        for benchmark in benchmarks
        if (min_hwp is None or benchmark.points >= min_hwp)
        and (max_hwp is None or benchmark.points <= max_hwp)
    )


def calculate_hwp(
    url: str,
    progress: Callable[[str], None] | None = None,
    cooling_filter: str = "All cooling",
    min_hwp: Decimal | None = None,
    max_hwp: Decimal | None = None,
) -> CalculationResult:
    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    report("Validating HWBOT hardware URL")
    safe_url = validate_hwbot_url(url)
    if cooling_filter != "All cooling":
        report(f"Cooling filter enabled: {cooling_filter}")
    if min_hwp is not None or max_hwp is not None:
        report(f"HWP range enabled: {format_points(min_hwp) if min_hwp is not None else '-inf'} to {format_points(max_hwp) if max_hwp is not None else '+inf'}")
    if cooling_filter != "All cooling" and (min_hwp is not None or max_hwp is not None):
        report("Range is applied before cooling checks to reduce HWBOT requests")
    report("Downloading public hardware page")
    html = download_html(safe_url)
    target = parse_hardware_target(html, safe_url)
    if target is not None:
        report(f"Detected {target.hardware_type}: {target.name}")

        # If a cooling filter is selected, we must scan ranking pages to find the
        # best HWP for that cooling, as the overview AJAX table only lists global #1s.
        if cooling_filter != "All cooling":
            report(f"Scanning ranking pages for {cooling_filter} results")
            cooling_id = COOLING_IDS.get(cooling_filter)
            benchmarks = scan_public_rankings_for_hardware(target, cooling_id=cooling_id, cooling_name=cooling_filter, progress=report)
            benchmarks = apply_hwp_range_filter(benchmarks, min_hwp, max_hwp)
            if not benchmarks:
                raise CalculatorError(f"No {cooling_filter} results found for the scanned benchmarks.")
            return CalculationResult(target.name, safe_url, benchmarks)

        ajax_url = build_hardware_ajax_url(target, safe_url)
        if ajax_url is not None:
            report("Loading HWBOT hardware-record table")
            fragment_html = download_html(ajax_url, ajax=True)
            report("Parsing benchmark HWP values")
            result = parse_hardware_records_fragment(fragment_html, ajax_url, target=target)
            benchmarks = apply_hwp_range_filter(result.benchmarks, min_hwp, max_hwp)
            if not benchmarks:
                raise CalculatorError("No benchmarks matched the selected HWP range.")
            benchmarks = apply_cooling_filter(
                benchmarks,
                target,
                cooling_filter,
                progress=progress,
            )
            if not benchmarks:
                raise CalculatorError(
                    "No benchmarks matched those filters. Try All cooling, widen the HWP range, "
                    "or clear the range fields."
                )
            return CalculationResult(result.title, result.page_url, benchmarks)

    try:
        report("Parsing visible page tables")
        result = parse_hwbot_page(html, safe_url)
        benchmarks = apply_hwp_range_filter(result.benchmarks, min_hwp, max_hwp)
        if not benchmarks:
            raise CalculatorError("No benchmarks matched the selected HWP range.")
        return CalculationResult(result.title, result.page_url, benchmarks)
    except CalculatorError:
        if target is None:
            raise

        report("Direct table unavailable; scanning public ranking pages")
        benchmarks = scan_public_rankings_for_hardware(target, progress=report)
        if benchmarks:
            benchmarks = apply_hwp_range_filter(benchmarks, min_hwp, max_hwp)
            if not benchmarks:
                raise CalculatorError("No benchmarks matched the selected HWP range.")
            return CalculationResult(
                title=target.name,
                page_url=safe_url,
                benchmarks=benchmarks,
            )

        raise CalculatorError(
            "The HWBOT hardware overview loaded correctly, but it does not contain a points table. "
            "I also checked public ranking pages and did not find visible HWP rows for this exact hardware "
            "within the scan limit. This is not necessarily a login problem; the results may be deeper in "
            "HWBOT's rankings or HWBOT may have changed its filters."
        )


class HwpCalculatorApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(APP_TITLE)
        self.geometry("1100x820")
        self.minsize(900, 600)

        self.worker_thread: threading.Thread | None = None
        self.loading = False
        self.loading_frame = 0
        self.cooling_warned = False
        self.expanded_groups: set[Decimal] = set()
        self.current_filter_summary = "Filters: all cooling, all HWP values"
        self.current_result: CalculationResult | None = None
        self.all_expanded = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self.build_header()
        self.build_input_area()
        self.build_results_area()
        self.build_status_bar()
        self.verify_integrity()
        
        # Create a context menu for the URL entry
        self.url_context_menu = tk.Menu(self, tearoff=0)
        self.url_context_menu.add_command(label="Cut", command=lambda: self.url_entry.event_generate("<<Cut>>"))
        self.url_context_menu.add_command(label="Copy", command=lambda: self.url_entry.event_generate("<<Copy>>"))
        self.url_context_menu.add_command(label="Paste", command=self.paste_to_url)
        self.url_context_menu.add_separator()
        self.url_context_menu.add_command(label="Select All", command=self.select_all_url)

    def build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text=APP_TITLE,
            font=ctk.CTkFont(size=28, weight="bold"),
            anchor="w",
        )
        title.grid(row=0, column=0, sticky="ew")

        subtitle = ctk.CTkLabel(
            header,
            text="Paste a public HWBOT CPU, GPU, or motherboard URL to group available hardware points.",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=14),
            anchor="w",
        )
        subtitle.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.update_button = ctk.CTkButton(
            header,
            text="Check for Updates",
            width=140,
            height=32,
            fg_color="#1E293B",
            hover_color="#334155",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.open_github
        )
        self.update_button.grid(row=0, column=1, rowspan=2, sticky="ne", pady=(5, 0))

    def build_input_area(self) -> None:
        panel = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color="#334155", fg_color="#1E293B")
        panel.grid(row=1, column=0, sticky="ew", padx=28, pady=14)
        panel.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            panel,
            height=44,
            placeholder_text="Paste HWBOT URL here...",
            fg_color="#0F172A",
            border_color="#334155",
            font=ctk.CTkFont(size=14),
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=16)
        
        self.url_entry.bind("<Return>", lambda _event: self.on_calculate())
        self.url_entry.bind("<Button-3>", self.show_url_menu)  # Right click
        self.url_entry._entry.bind("<Control-v>", lambda e: self.paste_to_url(e))
        self.url_entry._entry.bind("<Control-V>", lambda e: self.paste_to_url(e))
        self.url_entry._entry.bind("<Control-a>", lambda e: self.select_all_url(e))
        self.url_entry._entry.bind("<Control-A>", lambda e: self.select_all_url(e))

        self.button_container = ctk.CTkFrame(panel, fg_color="transparent")
        self.button_container.grid(row=0, column=1, sticky="e", padx=(0, 16), pady=16)

        self.clear_button = ctk.CTkButton(
            self.button_container,
            text="Clear",
            width=80,
            height=44,
            fg_color="#3D4654",
            hover_color="#4E596A",
            command=self.on_clear,
            font=ctk.CTkFont(size=14),
        )
        self.clear_button.pack(side="left", padx=(0, 8))

        self.calculate_button = ctk.CTkButton(
            self.button_container,
            text="Calculate",
            width=130,
            height=44,
            command=self.on_calculate,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.calculate_button.pack(side="left")

        filter_frame = ctk.CTkFrame(panel, fg_color="transparent")
        filter_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 16))
        filter_frame.grid_columnconfigure(1, weight=1)
        filter_frame.grid_columnconfigure(3, weight=0)
        filter_frame.grid_columnconfigure(5, weight=0)

        ctk.CTkLabel(
            filter_frame,
            text="Cooling",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.cooling_menu = ctk.CTkOptionMenu(
            filter_frame,
            values=["All cooling"] + list(COOLING_IDS.keys()),
            width=170,
            fg_color="#1E293B",
            command=self.on_cooling_change,
        )
        self.cooling_menu.set("All cooling")
        self.cooling_menu.grid(row=0, column=1, sticky="w", padx=(0, 22))

        ctk.CTkLabel(
            filter_frame,
            text="HWP from",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))

        self.min_hwp_entry = ctk.CTkEntry(
            filter_frame,
            width=92,
            fg_color="#0F172A",
            border_color="#334155",
            placeholder_text="min",
            font=ctk.CTkFont(size=13),
        )
        self.min_hwp_entry.grid(row=0, column=3, sticky="w", padx=(0, 12))

        ctk.CTkLabel(
            filter_frame,
            text="to",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=4, sticky="w", padx=(0, 8))

        self.max_hwp_entry = ctk.CTkEntry(
            filter_frame,
            width=92,
            fg_color="#0F172A",
            border_color="#334155",
            placeholder_text="max",
            font=ctk.CTkFont(size=13),
        )
        self.max_hwp_entry.grid(row=0, column=5, sticky="w")

    def build_results_area(self) -> None:
        body = ctk.CTkFrame(self, corner_radius=12, border_width=1, border_color="#334155", fg_color="#1E293B")
        body.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(6, weight=1)

        self.summary_label = ctk.CTkLabel(
            body,
            text="Results will appear here.",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w",
            cursor="hand2",
        )
        self.summary_label.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 4))
        self.summary_label.bind("<Button-1>", lambda _: self.open_current_url())

        self.total_panel = ctk.CTkFrame(body, corner_radius=12, fg_color="#0F172A", border_width=1, border_color="#334155")
        self.total_panel.grid(row=1, column=0, sticky="ew", padx=18, pady=(8, 10))
        self.total_panel.grid_columnconfigure(0, weight=1) # Text area
        self.total_panel.grid_columnconfigure(1, weight=0) # Button area
        self.total_panel.grid_rowconfigure((0, 1), weight=1)

        self.total_caption_label = ctk.CTkLabel(
            self.total_panel,
            text="Potential Total",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self.total_caption_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))

        self.total_value_label = ctk.CTkLabel(
            self.total_panel,
            text="-- HWP",
            font=ctk.CTkFont(size=24, weight="bold"),
            anchor="w",
        )
        self.total_value_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.action_container = ctk.CTkFrame(self.total_panel, fg_color="transparent")
        self.action_container.grid(row=0, column=1, rowspan=2, sticky="e", padx=12)

        self.export_button = ctk.CTkButton(
            self.action_container,
            text="Export CSV",
            width=90,
            height=32,
            fg_color="#10B981",
            hover_color="#059669",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.on_export_csv,
            state="disabled"
        )
        self.export_button.pack(side="left", padx=4, pady=12)

        self.copy_summary_button = ctk.CTkButton(
            self.action_container,
            text="Copy Summary",
            width=90,
            height=32,
            fg_color="#3B82F6",
            hover_color="#2563EB",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.on_copy_summary
        )
        self.copy_summary_button.pack(side="left", padx=4, pady=12)

        self.detail_label = ctk.CTkLabel(
            body,
            text="",
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=13),
            anchor="w",
        )
        self.detail_label.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 10))

        self.activity_log = ctk.CTkTextbox(
            body,
            height=112,
            corner_radius=8,
            fg_color="#020617",
            border_width=1,
            border_color="#1E293B",
            text_color="#A9C7E8",
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.activity_log.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.activity_log.insert("end", "system > ready to calculate\n")
        self.activity_log.configure(state="disabled")

        self.progress_bar = ctk.CTkProgressBar(body, height=2, corner_radius=0, fg_color="#1E293B", progress_color="#3B82F6")
        self.progress_bar.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.progress_bar.set(0)

        self.sticky_header = ctk.CTkFrame(body, fg_color="#1E293B", corner_radius=8)
        self.sticky_header.grid(row=5, column=0, sticky="ew", padx=24, pady=(0, 4))
        self.sticky_header.grid_columnconfigure(0, weight=0, minsize=150)
        self.sticky_header.grid_columnconfigure(1, weight=0, minsize=155)
        self.sticky_header.grid_columnconfigure(2, weight=0, minsize=140)
        self.sticky_header.grid_columnconfigure(3, weight=1)

        self.add_cell(self.sticky_header, "Available points", 0, bold=True, offset=40)
        self.add_cell(self.sticky_header, "Benchmark count", 1, bold=True)
        self.add_cell(self.sticky_header, "Bucket total", 2, bold=True)
        self.add_cell(self.sticky_header, "Breakdown", 3, bold=True)

        self.expand_all_btn = ctk.CTkButton(
            self.sticky_header,
            text="Expand All",
            width=90,
            height=28,
            fg_color="#2D3748",
            hover_color="#4A5568",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.on_toggle_all,
            state="disabled"
        )
        self.expand_all_btn.grid(row=0, column=3, sticky="e", padx=14)

        self.result_scroll = ctk.CTkScrollableFrame(body, corner_radius=8, fg_color="transparent")
        self.result_scroll.grid(row=6, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.result_scroll.grid_columnconfigure(0, weight=1)

        self.show_empty_state(
            "Paste a HWBOT hardware URL and press Calculate.",
            "The app only downloads the public page you provide and reads visible point values.",
        )

    def build_status_bar(self) -> None:
        status_bar = ctk.CTkFrame(self, fg_color="transparent")
        status_bar.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 18))
        status_bar.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            status_bar,
            text="Ready",
            text_color="#64748B",
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        # Signature verification: Designed & Developed by @hxntxihero
        sig = base64.b64decode("RGVzaWduZWQgJiBEZXZlbG9wZWQgYnkgQGh4bnR4aWhlcm8=").decode()
        self.watermark_label = ctk.CTkLabel(
            status_bar,
            text=sig,
            text_color="#475569",
            font=ctk.CTkFont(size=11, slant="italic"),
            anchor="e",
        )
        self.watermark_label.grid(row=0, column=1, sticky="e")

    def verify_integrity(self) -> None:
        sig = base64.b64decode("RGVzaWduZWQgJiBEZXZlbG9wZWQgYnkgQGh4bnR4aWhlcm8=").decode()
        if not hasattr(self, "watermark_label") or self.watermark_label.cget("text") != sig:
            self.calculate_button.configure(state="disabled", text="Tampered Version")
            self.status_label.configure(text="Error: Credit watermark tampered with.", text_color="red")

    def clear_results(self) -> None:
        for child in self.result_scroll.winfo_children():
            child.destroy()

    def reset_activity_log(self) -> None:
        self.activity_log.configure(state="normal")
        self.activity_log.delete("1.0", "end")
        self.activity_log.insert("end", "start > calculation requested\n")
        self.activity_log.configure(state="disabled")

    def append_activity_log(self, message: str) -> None:
        self.activity_log.configure(state="normal")
        self.activity_log.insert("end", f"hwpcalc > {message}\n")
        self.activity_log.see("end")
        self.activity_log.configure(state="disabled")

    def progress(self, message: str) -> None:
        self.after(0, lambda message=message: self.append_activity_log(message))

    def show_empty_state(self, title: str, message: str) -> None:
        if hasattr(self, "sticky_header"):
            self.sticky_header.grid_remove()
        self.clear_results()
        empty = ctk.CTkFrame(self.result_scroll, fg_color="transparent")
        empty.grid(row=0, column=0, sticky="nsew", padx=10, pady=34)
        empty.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            empty,
            text=title,
            font=ctk.CTkFont(size=16, weight="bold"),
            justify="center",
        ).grid(row=0, column=0, pady=(0, 8))

        ctk.CTkLabel(
            empty,
            text=message,
            text_color="#AAB2C0",
            font=ctk.CTkFont(size=13),
            justify="center",
            wraplength=620,
        ).grid(row=1, column=0)

    def set_busy(self, busy: bool) -> None:
        self.loading = busy
        self.calculate_button.configure(state="disabled" if busy else "normal")
        self.clear_button.configure(state="disabled" if busy else "normal")
        self.url_entry.configure(state="disabled" if busy else "normal")
        self.cooling_menu.configure(state="disabled" if busy else "normal")
        self.min_hwp_entry.configure(state="disabled" if busy else "normal")
        self.max_hwp_entry.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress_bar.set(0.05) # Show a small hint of progress
            self.progress_bar.configure(mode="determinate")
            self.loading_frame = 0
            self.animate_loading()
        else:
            self.progress_bar.set(1.0)
            self.status_label.configure(text="Calculation Complete. Ready.")
            self.url_entry.focus_set()

    def show_url_menu(self, event: tk.Event) -> None:
        self.url_context_menu.tk_popup(event.x_root, event.y_root)

    def paste_to_url(self, event: tk.Event | None = None) -> str:
        try:
            content = self.clipboard_get()
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, content)
        except tk.TclError:
            pass
        return "break"

    def select_all_url(self, event: tk.Event | None = None) -> str:
        self.url_entry.select_range(0, tk.END)
        self.url_entry.icursor(tk.END)
        self.url_entry.focus_set()
        return "break"

    def animate_loading(self) -> None:
        if not self.loading:
            return

        frames = ("Fetching HWBOT page", "Fetching HWBOT page.", "Fetching HWBOT page..", "Fetching HWBOT page...")
        self.status_label.configure(text=frames[self.loading_frame % len(frames)])
        self.loading_frame += 1
        self.after(300, self.animate_loading)

    def on_cooling_change(self, choice: str) -> None:
        if choice != "All cooling" and not self.cooling_warned:
            messagebox.showwarning(
                "Experimental Feature",
                "The Cooling Filter is currently in beta. It may not work accurately for all "
                "CPU/GPU models and will be improved in a future update.\n\n"
                "By clicking OK, you understand the current limitations."
            )
            self.cooling_warned = True

    def on_clear(self) -> None:
        self.url_entry.delete(0, tk.END)
        self.min_hwp_entry.delete(0, tk.END)
        self.max_hwp_entry.delete(0, tk.END)
        self.cooling_menu.set("All cooling")
        self.sticky_header.grid_remove()
        self.clear_results()
        self.summary_label.configure(text="Results cleared.")
        self.detail_label.configure(text="")
        self.total_value_label.configure(text="-- HWP")
        self.show_empty_state("Ready", "Paste a HWBOT hardware URL and press Calculate.")
        self.current_url = None
        self.current_result = None
        self.export_button.configure(state="disabled")
        self.expand_all_btn.configure(state="disabled")

    def open_current_url(self) -> None:
        if hasattr(self, "current_url") and self.current_url:
            webbrowser.open_new_tab(self.current_url)

    def open_github(self) -> None:
        webbrowser.open_new_tab(GITHUB_URL)

    def on_toggle_all(self) -> None:
        if not hasattr(self, "point_to_benchmarks"):
            return
        self.all_expanded = not self.all_expanded
        sorted_points = sorted(self.point_to_benchmarks.keys(), reverse=True)
        for points in sorted_points:
            if self.all_expanded:
                self.expand_group(points, self.get_row_index(points))
            else:
                self.collapse_group(points)
        self.expand_all_btn.configure(text="Collapse All" if self.all_expanded else "Expand All")

    def on_export_csv(self) -> None:
        if not self.current_result:
            return
        
        self.verify_integrity()
        if self.calculate_button.cget("text") == "Tampered Version":
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"{self.current_result.title.replace(' ', '_')}_HWP.csv"
        )
        
        if file_path:
            try:
                with open(file_path, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Benchmark", "HWP", "Cooling", "Submission URL"])
                    for bp in self.current_result.benchmarks:
                        writer.writerow([bp.benchmark, bp.points, bp.cooling or "N/A", bp.submission_url or "N/A"])
                messagebox.showinfo("Export Successful", f"Data exported to {file_path}")
            except Exception as e:
                messagebox.showerror("Export Failed", f"An error occurred: {e}")

    def on_copy_summary(self) -> None:
        if not self.current_result:
            return
        summary = (
            f"HWBOT HWP Summary: {self.current_result.title}\n"
            f"Total Potential: {format_points(self.current_result.total_potential)} HWP\n"
            f"Benchmarks found: {len(self.current_result.benchmarks)}\n"
            f"Calculated via HWBOT HWP Calculator (@hxntxihero)"
        )
        self.clipboard_clear()
        self.clipboard_append(summary)
        self.status_label.configure(text="Summary copied to clipboard!")

    def on_calculate(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        self.verify_integrity()
        url = self.url_entry.get()
        cooling_filter = self.cooling_menu.get()
        min_hwp_text = self.min_hwp_entry.get()
        max_hwp_text = self.max_hwp_entry.get()
        range_label = "all HWP values"
        if min_hwp_text.strip() or max_hwp_text.strip():
            range_label = f"{min_hwp_text.strip() or '-inf'} to {max_hwp_text.strip() or '+inf'} HWP"
        self.current_filter_summary = f"Filters: {cooling_filter}, {range_label}"

        try:
            min_hwp = parse_optional_decimal(min_hwp_text)
            max_hwp = parse_optional_decimal(max_hwp_text)
            if min_hwp is not None and max_hwp is not None and min_hwp > max_hwp:
                raise CalculatorError("Minimum HWP cannot be higher than maximum HWP.")
        except CalculatorError as exc:
            self.summary_label.configure(text="Check your filters")
            self.detail_label.configure(text="")
            self.total_value_label.configure(text="-- HWP")
            self.reset_activity_log()
            self.show_error(str(exc))
            return

        self.summary_label.configure(text="Calculating...")
        self.detail_label.configure(text="")
        self.total_caption_label.configure(text="Potential Total")
        self.total_value_label.configure(text="-- HWP")
        self.reset_activity_log()
        self.show_empty_state(
            "Downloading and parsing the page...",
            "The app is loading HWBOT's public hardware-record table for this hardware.",
        )
        self.set_busy(True)

        self.worker_thread = threading.Thread(
            target=self.run_calculation,
            args=(url, cooling_filter, min_hwp, max_hwp),
            daemon=True,
        )
        self.worker_thread.start()

    def run_calculation(
        self,
        url: str,
        cooling_filter: str,
        min_hwp: Decimal | None,
        max_hwp: Decimal | None,
    ) -> None:
        try:
            result = calculate_hwp(
                url,
                progress=self.progress,
                cooling_filter=cooling_filter,
                min_hwp=min_hwp,
                max_hwp=max_hwp,
            )
        except CalculatorError as exc:
            message = str(exc)
            self.after(0, lambda message=message: self.show_error(message))
        except Exception as exc:  # Defensive: keep the GUI alive for unexpected parser changes.
            message = f"Unexpected parser error: {exc}. HWBOT may have changed its page layout."
            self.after(
                0,
                lambda message=message: self.show_error(message),
            )
        else:
            self.after(0, lambda: self.show_result(result))

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        self.summary_label.configure(text="Could not calculate HWP")
        self.detail_label.configure(text="")
        self.total_value_label.configure(text="-- HWP")
        self.append_activity_log(f"error > {message}")
        self.show_empty_state("No points were calculated.", message)

    def show_result(self, result: CalculationResult) -> None:
        self.set_busy(False)
        self.clear_results()
        self.expanded_groups.clear()
        self.current_url = result.page_url
        self.current_result = result
        self.export_button.configure(state="normal")
        self.expand_all_btn.configure(state="normal")
        self.all_expanded = False
        self.expand_all_btn.configure(text="Expand All")
        self.display_benchmarks(result.benchmarks)

    def display_benchmarks(self, benchmarks: tuple[BenchmarkPoint, ...] | list[BenchmarkPoint]) -> None:
        self.clear_results()
        self.expanded_groups.clear()
        
        if not benchmarks:
            self.show_empty_state("No benchmarks found", "Try adjusting your search filter.")
            return

        # Group points
        self.point_to_benchmarks: dict[Decimal, list[BenchmarkPoint]] = {}
        for bp in benchmarks:
            self.point_to_benchmarks.setdefault(bp.points, []).append(bp)

        sorted_points = sorted(self.point_to_benchmarks.keys(), reverse=True)
        
        total_pts = sum((b.points for b in benchmarks), Decimal("0"))
        if self.current_result:
            self.summary_label.configure(
                text=f"{self.current_result.title} - {len(benchmarks)} benchmarks found"
            )
        self.total_value_label.configure(text=f"{format_points(total_pts)} HWP")

        self.detail_label.configure(
            text=f"{self.current_filter_summary}. Grouped from HWBOT's public hardware-record table."
        )
        self.append_activity_log(f"done > total potential is {format_points(total_pts)} HWP")

        self.sticky_header.grid()
        for i, points in enumerate(sorted_points):
            benchmarks = self.point_to_benchmarks[points]
            count = len(benchmarks)
            
            # Main group row
            row_color = "#0F172A" if i % 2 else "#1E293B"
            row = self.create_row(row_color)
            row.grid(row=i*2 + 1, column=0, sticky="ew", padx=6, pady=(3, 0))

            point_label = f"{format_points(points)} HWP"
            count_label = f"{count} benchmark" if count == 1 else f"{count} benchmarks"
            bucket_total_val = points * count
            
            # Add expand icon/button
            expand_btn = ctk.CTkButton(
                row, text="", width=30, height=30, fg_color="transparent", 
                hover_color="#334155", font=ctk.CTkFont(size=14),
                command=lambda p=points, r=i*2+2, b=None: self.toggle_group(p, r)
            )
            expand_btn.grid(row=0, column=0, sticky="w", padx=(5, 0))
            setattr(self, f"btn_{points}", expand_btn) # Store reference to change text later

            self.add_cell(row, point_label, 0, bold=True, offset=40)
            self.add_cell(row, count_label, 1)
            self.add_cell(row, f"{format_points(bucket_total_val)} HWP", 2, bold=True)
            self.add_cell(row, f"{format_points(points)} x {count} = {format_points(bucket_total_val)} HWP", 3)

            detail_frame = ctk.CTkFrame(self.result_scroll, fg_color="#020617", corner_radius=0)
            setattr(self, f"detail_{points}", detail_frame)

    def get_row_index(self, points: Decimal) -> int:
        sorted_points = sorted(self.point_to_benchmarks.keys(), reverse=True)
        return sorted_points.index(points) * 2 + 2

    def toggle_group(self, points: Decimal, row_idx: int) -> None:
        if points in self.expanded_groups:
            self.collapse_group(points)
        else:
            self.expand_group(points, row_idx)

    def collapse_group(self, points: Decimal) -> None:
        frame = getattr(self, f"detail_{points}", None)
        btn = getattr(self, f"btn_{points}")
        if frame:
            frame.grid_forget()
        if points in self.expanded_groups:
            self.expanded_groups.remove(points)
        btn.configure(text="")

    def expand_group(self, points: Decimal, row_idx: int) -> None:
        frame = getattr(self, f"detail_{points}")
        btn = getattr(self, f"btn_{points}")
        frame.grid(row=row_idx, column=0, sticky="ew", padx=20, pady=(0, 5))
        self.expanded_groups.add(points)
        btn.configure(text="")
            
        if not frame.winfo_children():
            header = ctk.CTkFrame(frame, fg_color="#0F172A", corner_radius=6)
            header.pack(fill="x", padx=10, pady=(5, 2))
            ctk.CTkLabel(header, text="Benchmark Name", font=ctk.CTkFont(size=11, weight="bold"), text_color="#64748B").pack(side="left", padx=15)
            ctk.CTkLabel(header, text="Action", font=ctk.CTkFont(size=11, weight="bold"), text_color="#64748B").pack(side="right", padx=15)

            for bp in self.point_to_benchmarks[points]:
                bench_row = ctk.CTkFrame(frame, fg_color="transparent")
                bench_row.pack(fill="x", padx=10, pady=1)
                
                display_text = f"• {bp.benchmark}"
                if bp.cooling:
                    display_text += f" ({bp.cooling})"

                name_lbl = ctk.CTkLabel(bench_row, text=display_text, font=ctk.CTkFont(size=13))
                name_lbl.pack(side="left", padx=10)
                
                if bp.submission_url:
                    btn = ctk.CTkButton(
                        bench_row, text="View Link ↗", width=100, height=24,
                        font=ctk.CTkFont(size=11), fg_color="#1E293B", hover_color="#334155",
                        command=lambda url=bp.submission_url: webbrowser.open_new_tab(url)
                    )
                    btn.pack(side="right", padx=10)

    def create_row(self, color: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self.result_scroll, fg_color=color, corner_radius=8)
        row.grid_columnconfigure(0, weight=0, minsize=150)
        row.grid_columnconfigure(1, weight=0, minsize=155)
        row.grid_columnconfigure(2, weight=0, minsize=140)
        row.grid_columnconfigure(3, weight=1)
        return row

    def add_cell(
        self, 
        parent: ctk.CTkFrame, 
        text: str, 
        column: int, 
        bold: bool = False, 
        offset: int = 14
    ) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=13, weight="bold" if bold else "normal"),
            anchor="w",
            justify="left",
            wraplength=300 if column == 3 else 135,
        ).grid(row=0, column=column, sticky="ew", padx=(offset, 14), pady=12)


def main() -> None:
    app = HwpCalculatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
