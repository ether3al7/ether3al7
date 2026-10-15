#!/usr/bin/env python3
"""Self-hosted GitHub statistics generator for the ether3al7 profile README.

Produces two dark, GitHub-styled SVG cards:

    profile/github-stats.svg   "GitHub Engineering Activity"
    profile/top-langs.svg      "Languages Across My GitHub Work"

Scope of every number on the cards:

    * activity GitHub can actually attribute to the user ether3al7, and
    * repositories owned by ether3al7, plus
    * repositories owned by LionsToothOrg where ether3al7 is a real contributor.

Organization-wide activity is never credited to the user: activity metrics are
built from `author:`/`reviewed-by:` search qualifiers, and the organization's
repositories only enter the language totals when the contributor list for that
repository actually contains ether3al7.

Privacy rules (this generator runs in a public repository):

    * The token is read from the environment only. It is never printed, logged,
      embedded in output, or written to disk.
    * Private repository names, metadata, and per-repository statistics never
      reach the SVGs. Only aggregates are published.
    * Console output names public repositories only. Private ones are counted
      but reported as "<private repository>".

Usage:
    PROFILE_STATS_TOKEN=... python scripts/profile_stats.py
    python scripts/profile_stats.py --demo      # render sample cards, no token
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

API_ROOT = "https://api.github.com"
USERNAME = "ether3al7"
ORG = "LionsToothOrg"

# Languages excluded from the calculation entirely.
EXCLUDED_LANGUAGES = {"Jupyter Notebook"}

TOP_N = 8
ACTIVITY_WINDOW_DAYS = 365

TOKEN_ENV_VARS = ("PROFILE_STATS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# Search endpoints have a much lower rate limit (30 requests/minute), so space
# them out a little rather than risking a secondary-limit penalty.
SEARCH_DELAY_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

CARD_WIDTH = 495
BG = "#0d1117"
BORDER = "#30363d"
ACCENT = "#58a6ff"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
TRACK = "#21262d"

FONT = "'Segoe UI',Ubuntu,-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"

LANGUAGE_COLORS = {
    "Python": "#3572A5",
    "Java": "#b07219",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "SCSS": "#c6538c",
    "Less": "#1d365d",
    "Shell": "#89e051",
    "HCL": "#844FBA",
    "Dockerfile": "#384d54",
    "C": "#555555",
    "C++": "#f34b7d",
    "C#": "#178600",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
    "Kotlin": "#A97BFF",
    "Swift": "#F05138",
    "Dart": "#00B4AB",
    "Vue": "#41b883",
    "Svelte": "#ff3e00",
    "Astro": "#ff5a03",
    "Makefile": "#427819",
    "CMake": "#DA3434",
    "PowerShell": "#012456",
    "Batchfile": "#C1F12E",
    "SQL": "#e38c00",
    "PLpgSQL": "#336790",
    "TSQL": "#e38c00",
    "Handlebars": "#f7931e",
    "EJS": "#a91e50",
    "Mustache": "#724b3b",
    "Lua": "#000080",
    "Perl": "#0298c3",
    "R": "#198CE7",
    "Scala": "#c22d40",
    "Groovy": "#4298b8",
    "Gradle": "#02303a",
    "YAML": "#cb171e",
    "Markdown": "#083fa1",
    "Objective-C": "#438eff",
    "Assembly": "#6E4C13",
    "Vim Script": "#199f4b",
    "Elixir": "#6e4a7e",
    "Haskell": "#5e5086",
    "Clojure": "#db5855",
    "Zig": "#ec915c",
    "GDScript": "#355570",
    "Smarty": "#f0c040",
    "Procfile": "#8b949e",
}

FALLBACK_COLORS = [
    "#58a6ff",
    "#3fb950",
    "#d29922",
    "#f85149",
    "#a371f7",
    "#39c5cf",
    "#db6d28",
    "#8b949e",
]


class GitHubError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


def _scrub(text: str) -> str:
    """Defensive: make sure a token can never surface in a message we print."""
    for name in TOKEN_ENV_VARS:
        secret = os.environ.get(name, "").strip()
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


def _is_rate_limited(headers: Dict[str, str]) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("retry-after") is not None or lowered.get("x-ratelimit-remaining") == "0"


def _retry_after_seconds(headers: Dict[str, str], attempt: int) -> float:
    lowered = {k.lower(): v for k, v in headers.items()}
    retry_after = lowered.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min((2 ** attempt) * 5, 60.0)


class GitHubClient:
    """Minimal GitHub REST client: pagination, retries, rate-limit awareness."""

    def __init__(self, token: str, user_agent: str = "ether3al7-profile-stats") -> None:
        self._token = token
        self._user_agent = user_agent
        self._last_search = 0.0

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
            "Authorization": f"Bearer {self._token}",
        }

    def request(
        self, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 3
    ) -> Tuple[int, Any, Dict[str, str]]:
        """Return (status, parsed_body_or_None, headers).

        Expected "no" answers (403/404/204/409/422/451) are returned rather than
        raised so callers can degrade gracefully instead of inventing numbers.
        """
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    raw = resp.read()
                    body = json.loads(raw) if raw else None
                    return resp.status, body, dict(resp.headers)
            except urllib.error.HTTPError as exc:
                headers = dict(exc.headers or {})
                if exc.code in (429, 403) and _is_rate_limited(headers):
                    if attempt < retries - 1:
                        time.sleep(_retry_after_seconds(headers, attempt))
                        continue
                if exc.code in (204, 403, 404, 409, 422, 451):
                    return exc.code, None, headers
                if exc.code >= 500 and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"GitHub API {exc.code} for {_scrub(url)}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
        raise GitHubError(f"Network failure calling {_scrub(url)}: {last_error}")

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        status, body, _ = self.request(path, params)
        if status >= 400 or body is None:
            raise GitHubError(f"Unexpected status {status} for {_scrub(path)}")
        return body

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterable[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while True:
            params["page"] = page
            status, body, _ = self.request(path, params)
            if status >= 400 or not body:
                return
            if not isinstance(body, list):
                raise GitHubError(f"Expected a list from {_scrub(path)}")
            for item in body:
                yield item
            if len(body) < params["per_page"]:
                return
            page += 1
            if page > 20:  # hard stop, well beyond any realistic repo count
                return

    def search_count(self, endpoint: str, query: str) -> Optional[int]:
        """Return total_count for a search query, or None if GitHub refused.

        `endpoint` is "issues" or "commits". total_count is reliable even though
        search result *pages* are capped at 1000 items.
        """
        elapsed = time.monotonic() - self._last_search
        if elapsed < SEARCH_DELAY_SECONDS:
            time.sleep(SEARCH_DELAY_SECONDS - elapsed)
        status, body, _ = self.request(
            f"/search/{endpoint}", {"q": query, "per_page": 1}
        )
        self._last_search = time.monotonic()
        if status >= 400 or not isinstance(body, dict):
            return None
        count = body.get("total_count")
        return int(count) if isinstance(count, int) else None


def read_token() -> str:
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            if name != TOKEN_ENV_VARS[0]:
                print(f"Using token from {name} (PROFILE_STATS_TOKEN not set).")
            return value
    raise SystemExit(
        "No token found. Set PROFILE_STATS_TOKEN in the environment. "
        "In CI the workflow passes ${{ secrets.PROFILE_STATS_TOKEN }}. "
        "Locally you can run: PROFILE_STATS_TOKEN=$(gh auth token) python scripts/profile_stats.py"
    )


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------


def is_eligible(repo: Dict[str, Any]) -> bool:
    """Non-fork, non-archived."""
    return not repo.get("fork", False) and not repo.get("archived", False)


def label(repo: Dict[str, Any]) -> str:
    """Log-safe identifier: public repos by name, private ones anonymized."""
    if repo.get("private", True):
        return "<private repository>"
    return repo.get("full_name", "<unknown>")


def collect_personal_repos(client: GitHubClient) -> Tuple[List[Dict[str, Any]], int]:
    """Eligible repositories owned by USERNAME. Returns (repos, total_seen)."""
    seen: Dict[str, Dict[str, Any]] = {}
    eligible: Dict[str, Dict[str, Any]] = {}

    def consider(repo: Dict[str, Any]) -> None:
        owner = (repo.get("owner") or {}).get("login", "")
        if owner.lower() != USERNAME.lower():
            return
        seen[repo["full_name"]] = repo
        if is_eligible(repo):
            eligible.setdefault(repo["full_name"], repo)

    # Authenticated listing surfaces private repositories the token can read.
    for repo in client.paginate(
        "/user/repos", {"affiliation": "owner", "visibility": "all", "sort": "full_name"}
    ):
        consider(repo)
    # Public listing as a safety net if the token lacks the `repo` scope.
    for repo in client.paginate(f"/users/{USERNAME}/repos", {"type": "owner"}):
        consider(repo)

    return sorted(eligible.values(), key=lambda r: r["full_name"]), len(seen)


def user_is_contributor(client: GitHubClient, full_name: str) -> Optional[bool]:
    """True/False when determinable, None when GitHub would not answer.

    Uses the contributors list, which is GitHub's own attribution of commit
    authorship (it resolves commit emails to accounts), then falls back to
    commit search by author for repositories whose contributor stats are not
    yet computed.
    """
    status, body, _ = client.request(
        f"/repos/{full_name}/contributors", {"per_page": 100, "anon": "0"}
    )
    if status == 204:  # empty repository
        return False
    if status >= 400 or not isinstance(body, list):
        return None
    for contributor in body:
        if (contributor.get("login") or "").lower() == USERNAME.lower():
            return True
    # The list is ordered by commit count and paginated; walk on if it was full.
    if len(body) == 100:
        for contributor in client.paginate(f"/repos/{full_name}/contributors", {"anon": "0"}):
            if (contributor.get("login") or "").lower() == USERNAME.lower():
                return True
    return False


def collect_org_contributions(
    client: GitHubClient,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Eligible ORG repositories where GitHub attributes commits to USERNAME."""
    diag: Dict[str, Any] = {
        "org_visible": False,
        "org_repos_seen": 0,
        "org_repos_eligible": 0,
        "org_private_seen": 0,
        "org_private_counted": 0,
        "contributor_checks_denied": 0,
        "skipped_not_contributor": 0,
        "skipped_fork_or_archived": 0,
    }

    status, _, _ = client.request(f"/orgs/{ORG}")
    if status >= 400:
        print(f"  {ORG} is not accessible with this token (status {status}).")
        return [], diag
    diag["org_visible"] = True

    contributed: List[Dict[str, Any]] = []
    for repo in client.paginate(f"/orgs/{ORG}/repos", {"type": "all", "sort": "full_name"}):
        diag["org_repos_seen"] += 1
        if repo.get("private"):
            diag["org_private_seen"] += 1
        if not is_eligible(repo):
            diag["skipped_fork_or_archived"] += 1
            continue
        diag["org_repos_eligible"] += 1

        verdict = user_is_contributor(client, repo["full_name"])
        if verdict is None:
            diag["contributor_checks_denied"] += 1
            print(f"    ? {label(repo)}: contributor list unavailable, not counted")
            continue
        if not verdict:
            diag["skipped_not_contributor"] += 1
            continue
        contributed.append(repo)
        if repo.get("private"):
            diag["org_private_counted"] += 1
    return contributed, diag


# ---------------------------------------------------------------------------
# Language aggregation
# ---------------------------------------------------------------------------


def aggregate_languages(
    client: GitHubClient, repos: Iterable[Dict[str, Any]]
) -> Tuple[Dict[str, int], int, int]:
    """Sum Linguist byte counts. Returns (totals, repos_counted, repos_failed)."""
    totals: Dict[str, int] = {}
    counted = 0
    failed = 0
    for repo in repos:
        status, body, _ = client.request(f"/repos/{repo['full_name']}/languages")
        if status >= 400 or not isinstance(body, dict):
            failed += 1
            print(f"    ! {label(repo)}: language data unavailable (status {status})")
            continue
        counted += 1
        for language, size in body.items():
            if language in EXCLUDED_LANGUAGES:
                continue
            try:
                value = int(size)
            except (TypeError, ValueError):
                continue
            if value > 0:
                totals[language] = totals.get(language, 0) + value
    return totals, counted, failed


def top_languages(totals: Dict[str, int], limit: int = TOP_N) -> List[Tuple[str, int, float]]:
    """Top `limit` languages as (name, bytes, percent-of-shown)."""
    if not totals:
        return []
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    shown_total = sum(size for _, size in ranked)
    if shown_total == 0:
        return []
    return [(name, size, size / shown_total * 100.0) for name, size in ranked]


# ---------------------------------------------------------------------------
# Activity metrics
#
# Every query is anchored to `author:`/`reviewed-by:` so only activity GitHub
# attributes to USERNAME is counted. Owner scopes are queried separately
# (`user:USERNAME` and `org:ORG`) and summed, so a missing/denied scope is
# visible instead of silently collapsing into one number.
# ---------------------------------------------------------------------------


def _sum_scoped(
    client: GitHubClient, endpoint: str, base_query: str
) -> Tuple[Optional[int], List[str]]:
    """Run `base_query` against each owner scope and sum. None if all scopes fail."""
    total = 0
    any_ok = False
    problems: List[str] = []
    for scope in (f"user:{USERNAME}", f"org:{ORG}"):
        count = client.search_count(endpoint, f"{base_query} {scope}")
        if count is None:
            problems.append(scope)
            continue
        any_ok = True
        total += count
    return (total if any_ok else None), problems


def collect_activity(
    client: GitHubClient, window_days: int = ACTIVITY_WINDOW_DAYS
) -> Tuple[Dict[str, Optional[int]], Dict[str, Any]]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).date().isoformat()
    diag: Dict[str, Any] = {"since": since, "unavailable": [], "partial_scopes": {}}
    metrics: Dict[str, Optional[int]] = {}

    queries: List[Tuple[str, str, str]] = [
        # key, search endpoint, query (owner scope appended per call)
        ("commits", "commits", f"author:{USERNAME} author-date:>={since}"),
        ("prs_opened", "issues", f"type:pr author:{USERNAME} created:>={since}"),
        ("prs_merged", "issues", f"type:pr author:{USERNAME} is:merged merged:>={since}"),
        ("issues_opened", "issues", f"type:issue author:{USERNAME} created:>={since}"),
        (
            "reviews",
            "issues",
            f"type:pr reviewed-by:{USERNAME} -author:{USERNAME} updated:>={since}",
        ),
    ]

    for key, endpoint, query in queries:
        value, problems = _sum_scoped(client, endpoint, query)
        metrics[key] = value
        if value is None:
            diag["unavailable"].append(key)
            print(f"    - {key}: unavailable from the search API, will be omitted")
        elif problems:
            diag["partial_scopes"][key] = problems
            print(f"    ~ {key}: {value} (incomplete, scope refused: {', '.join(problems)})")
        else:
            print(f"    - {key}: {value}")

    return metrics, diag


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------


def escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def color_for(language: str, index: int) -> str:
    return LANGUAGE_COLORS.get(language, FALLBACK_COLORS[index % len(FALLBACK_COLORS)])


def _card_open(width: int, height: int, title: str, dom_id: str) -> List[str]:
    return [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="{dom_id}">',
        f'<title id="{dom_id}">{escape(title)}</title>',
        "<style>"
        f".t{{font:600 18px {FONT};fill:{ACCENT};}}"
        f".kpi{{font:700 22px {FONT};fill:{TEXT};}}"
        f".kpi-label{{font:400 11px {FONT};fill:{MUTED};letter-spacing:.3px;}}"
        f".lang{{font:400 12px {FONT};fill:{TEXT};}}"
        f".pct{{font:600 12px {FONT};fill:{TEXT};}}"
        f".note{{font:400 10px {FONT};fill:{MUTED};}}"
        "</style>",
        f'<rect x="0.5" y="0.5" rx="8" width="{width - 1}" height="{height - 1}" '
        f'fill="{BG}" stroke="{BORDER}"/>',
    ]


def _format_count(value: int) -> str:
    if value >= 10000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value:,}"


def render_stats_svg(
    metrics: Dict[str, Optional[int]],
    repos_contributed: int,
    window_days: int,
    diag: Dict[str, Any],
    title: str = "GitHub Engineering Activity",
) -> str:
    months = max(1, round(window_days / 30.44))
    window_label = f"{months} mo" if months != 12 else "12 mo"

    # Only metrics GitHub actually returned make it onto the card. Labels carry
    # their own scope so nothing on the card can be read as an all-time total.
    candidates: List[Tuple[str, Optional[int], str]] = [
        ("commits", metrics.get("commits"), f"COMMITS · {window_label}"),
        ("prs_opened", metrics.get("prs_opened"), f"PULL REQUESTS · {window_label}"),
        ("prs_merged", metrics.get("prs_merged"), f"PRS MERGED · {window_label}"),
        ("issues_opened", metrics.get("issues_opened"), f"ISSUES OPENED · {window_label}"),
        ("reviews", metrics.get("reviews"), f"CODE REVIEWS · {window_label}"),
        ("repos", repos_contributed, "REPOS CONTRIBUTED TO"),
    ]
    partial = diag.get("partial_scopes", {})
    # Optional metrics are dropped when they are both zero and optional, so the
    # card never shows a metric that GitHub cannot meaningfully attribute.
    optional_when_zero = {"reviews", "issues_opened"}
    tiles: List[Tuple[str, str]] = []
    for key, value, text in candidates:
        if value is None:
            continue
        if value == 0 and key in optional_when_zero:
            continue
        suffix = "+" if key in partial else ""
        tiles.append((_format_count(int(value)) + suffix, text))

    padding = 25
    inner = CARD_WIDTH - padding * 2
    cols = 3
    col_w = inner / cols
    row_h = 62
    grid_top = 74
    rows = max(1, (len(tiles) + cols - 1) // cols)
    note_y = grid_top + rows * row_h + 4
    height = int(note_y + 22)

    parts = _card_open(CARD_WIDTH, height, title, "statsTitle")
    parts.append(f'<text x="{padding}" y="34" class="t">{escape(title)}</text>')
    parts.append(
        f'<text x="{padding}" y="52" class="note">'
        f"Attributed to {escape(USERNAME)} across personal repositories and "
        f"{escape(ORG)} contributions"
        "</text>"
    )
    parts.append(
        f'<line x1="{padding}" y1="60" x2="{CARD_WIDTH - padding}" y2="60" '
        f'stroke="{BORDER}" stroke-width="1"/>'
    )

    if not tiles:
        parts.append(
            f'<text x="{padding}" y="{grid_top + 12}" class="lang">'
            "Activity metrics are temporarily unavailable.</text>"
        )
    for index, (value, text) in enumerate(tiles):
        col = index % cols
        row = index // cols
        x = padding + col * col_w
        y = grid_top + row * row_h
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" class="kpi">{escape(value)}</text>')
        parts.append(
            f'<text x="{x:.1f}" y="{y + 18:.1f}" class="kpi-label">{escape(text)}</text>'
        )

    note = "Includes public and private work. Commit counts cover default branches."
    if partial:
        note = "Partial data (+): some scopes were unavailable. " + note
    parts.append(f'<text x="{padding}" y="{note_y + 12:.0f}" class="note">{escape(note)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_langs_svg(
    entries: List[Tuple[str, int, float]],
    title: str = "Languages Across My GitHub Work",
) -> str:
    padding = 25
    bar_w = CARD_WIDTH - padding * 2
    bar_y = 72
    bar_h = 9
    row_h = 26
    legend_y = bar_y + bar_h + 24
    rows = max(1, (len(entries) + 1) // 2)
    note_y = legend_y + rows * row_h + 2
    height = int(note_y + 20)

    parts = _card_open(CARD_WIDTH, height, title, "langsTitle")
    parts.append(f'<text x="{padding}" y="34" class="t">{escape(title)}</text>')
    parts.append(
        f'<text x="{padding}" y="52" class="note">'
        f"Aggregate Linguist bytes from repositories {escape(USERNAME)} owns or contributes to"
        "</text>"
    )
    parts.append(
        f'<line x1="{padding}" y1="60" x2="{CARD_WIDTH - padding}" y2="60" '
        f'stroke="{BORDER}" stroke-width="1"/>'
    )

    if not entries:
        parts.append(
            f'<text x="{padding}" y="{bar_y + 14}" class="lang">No language data available.</text>'
        )
        parts.append("</svg>")
        return "\n".join(parts) + "\n"

    parts.append(
        f'<clipPath id="barClip"><rect x="{padding}" y="{bar_y}" width="{bar_w}" '
        f'height="{bar_h}" rx="{bar_h / 2}"/></clipPath>'
    )
    parts.append(
        f'<rect x="{padding}" y="{bar_y}" width="{bar_w}" height="{bar_h}" '
        f'rx="{bar_h / 2}" fill="{TRACK}"/>'
    )
    parts.append('<g clip-path="url(#barClip)">')
    offset = 0.0
    for index, (name, _size, pct) in enumerate(entries):
        seg_w = bar_w * pct / 100.0
        if index == len(entries) - 1:
            seg_w = bar_w - offset  # absorb rounding so the bar is exactly full
        parts.append(
            f'<rect x="{padding + offset:.2f}" y="{bar_y}" width="{max(seg_w, 0):.2f}" '
            f'height="{bar_h}" fill="{color_for(name, index)}"/>'
        )
        offset += seg_w
    parts.append("</g>")

    col_w = bar_w / 2
    for index, (name, _size, pct) in enumerate(entries):
        col = index % 2
        row = index // 2
        x = padding + col * col_w
        y = legend_y + row * row_h
        parts.append(f'<circle cx="{x + 5:.1f}" cy="{y:.1f}" r="5" fill="{color_for(name, index)}"/>')
        parts.append(f'<text x="{x + 18:.1f}" y="{y + 4:.1f}" class="lang">{escape(name)}</text>')
        parts.append(
            f'<text x="{x + col_w - 14:.1f}" y="{y + 4:.1f}" class="pct" '
            f'text-anchor="end">{pct:.2f}%</text>'
        )

    parts.append(
        f'<text x="{padding}" y="{note_y + 12:.0f}" class="note">'
        f"Top {len(entries)} languages. Forks, archived repositories and "
        f"{escape(', '.join(sorted(EXCLUDED_LANGUAGES)))} excluded."
        "</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_svg(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    print(f"  wrote {path}")


def run_demo(stats_out: str, langs_out: str) -> int:
    """Render both cards from sample numbers so the layout can be reviewed
    locally without a token and without touching the GitHub API."""
    print("Demo mode: sample data, no API calls, no token used.")
    metrics: Dict[str, Optional[int]] = {
        "commits": 842,
        "prs_opened": 96,
        "prs_merged": 88,
        "issues_opened": 41,
        "reviews": 57,
    }
    sample = {
        "Python": 820_000,
        "TypeScript": 610_000,
        "Java": 540_000,
        "JavaScript": 390_000,
        "HCL": 180_000,
        "CSS": 120_000,
        "HTML": 96_000,
        "Shell": 44_000,
    }
    entries = top_languages(sample)
    write_svg(
        stats_out,
        render_stats_svg(metrics, 23, ACTIVITY_WINDOW_DAYS, {"partial_scopes": {}}),
    )
    write_svg(langs_out, render_langs_svg(entries))
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the self-hosted profile statistics SVG cards."
    )
    parser.add_argument(
        "--stats-out",
        default=os.path.join("profile", "github-stats.svg"),
        help="Activity card output path (default: profile/github-stats.svg)",
    )
    parser.add_argument(
        "--langs-out",
        default=os.path.join("profile", "top-langs.svg"),
        help="Language card output path (default: profile/top-langs.svg)",
    )
    parser.add_argument(
        "--top", type=int, default=TOP_N, help=f"Languages to show (default: {TOP_N})."
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=ACTIVITY_WINDOW_DAYS,
        help=f"Activity window in days (default: {ACTIVITY_WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Render both cards from sample data without calling the API.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.demo:
        return run_demo(args.stats_out, args.langs_out)

    token = read_token()
    client = GitHubClient(token)

    try:
        viewer = client.get_json("/user").get("login", "<unknown>")
    except GitHubError as exc:
        print(f"Could not authenticate: {exc}")
        return 1
    print(f"Authenticated as {viewer}.")
    if viewer.lower() != USERNAME.lower():
        print(
            f"  note: token identity is {viewer}, statistics are still scoped to {USERNAME}."
        )

    print(f"\n[1/4] Personal repositories owned by {USERNAME}")
    personal, personal_seen = collect_personal_repos(client)
    personal_private = sum(1 for r in personal if r.get("private"))
    print(f"  evaluated: {personal_seen}")
    print(f"  eligible (non-fork, non-archived): {len(personal)}")
    print(f"  of those private: {personal_private} (counted, never named)")

    print(f"\n[2/4] {ORG} repositories with attributed contributions")
    org_repos, org_diag = collect_org_contributions(client)
    print(f"  evaluated: {org_diag['org_repos_seen']} ({org_diag['org_private_seen']} private)")
    print(f"  eligible (non-fork, non-archived): {org_diag['org_repos_eligible']}")
    print(f"  contributor confirmed, counted: {len(org_repos)}")
    print(f"  skipped, not a contributor: {org_diag['skipped_not_contributor']}")
    if org_diag["contributor_checks_denied"]:
        print(f"  skipped, attribution unavailable: {org_diag['contributor_checks_denied']}")

    print("\n[3/4] Activity attributed to the user (search API)")
    metrics, activity_diag = collect_activity(client, args.window_days)

    print("\n[4/4] Language aggregation")
    all_repos = list(personal) + list(org_repos)
    totals, lang_counted, lang_failed = aggregate_languages(client, all_repos)
    entries = top_languages(totals, args.top)
    grand_total = sum(totals.values())
    print(f"  repositories with language data: {lang_counted} of {len(all_repos)}")
    if lang_failed:
        print(f"  repositories whose language data was unavailable: {lang_failed}")
    print(f"  distinct languages: {len(totals)}  |  total bytes: {grand_total:,}")
    for name, size, pct in entries:
        share_all = size / grand_total * 100.0 if grand_total else 0.0
        print(f"    {name:<16} {pct:6.2f}%  (of all bytes: {share_all:5.2f}%)")

    repos_contributed = len(all_repos)
    print("\nRendering cards")
    write_svg(
        args.stats_out,
        render_stats_svg(metrics, repos_contributed, args.window_days, activity_diag),
    )
    write_svg(args.langs_out, render_langs_svg(entries, "Languages Across My GitHub Work"))

    summary = {
        "window_start": activity_diag["since"],
        "personal_repos_evaluated": personal_seen,
        "personal_repos_counted": len(personal),
        "org_repos_evaluated": org_diag["org_repos_seen"],
        "org_repos_eligible": org_diag["org_repos_eligible"],
        "org_repos_contributed_counted": len(org_repos),
        "org_private_repos_visible": org_diag["org_private_seen"],
        "org_private_repos_counted": org_diag["org_private_counted"],
        "private_org_access_ok": org_diag["org_private_seen"] > 0,
        "repos_contributed_to": repos_contributed,
        "commits": metrics.get("commits"),
        "prs_opened": metrics.get("prs_opened"),
        "prs_merged": metrics.get("prs_merged"),
        "issues_opened": metrics.get("issues_opened"),
        "reviews": metrics.get("reviews"),
        "metrics_unavailable": activity_diag["unavailable"],
        "metrics_partial": activity_diag["partial_scopes"],
        "total_language_bytes": grand_total,
        "top_languages": [
            {"language": name, "percent": round(pct, 2)} for name, _size, pct in entries
        ],
    }
    print("\nSUMMARY " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
