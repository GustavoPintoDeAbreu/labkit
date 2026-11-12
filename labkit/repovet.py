"""Vet a GitHub repository before the lab installs or depends on it.

Fake projects (AI tools and ComfyUI custom nodes above all) look alive and useful and are
neither: automated commits that change nothing, a README that promises features the code
does not have, a download hidden behind a base64 string or a link shortener, and
instructions to switch off SmartScreen or the antivirus. This module looks for exactly
those signs.

It **reads through the GitHub API only**: a repository is never cloned, installed or run.
``scan_tree`` does the same content checks on a folder that is already on disk (a clone
the lab runs), reading text files only.

    report = vet("https://github.com/owner/name")
    report.verdict     # "ok" | "caution" | "red"
    report.summary()   # one line per sign

The pure checks (``scan_text``, ``commit_signals``, ``substance_signals``,
``identity_signals``) take plain data, so they are tested without a network. Used by
lab-audit (the daily "sources" step) and idea-pipeline (a PR that adds a new third-party
repository). CLI: ``labkit vet <url> [--ref REF] [--json]``.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

RED, CAUTION, INFO = "red", "caution", "info"
API = "https://api.github.com"
# Text files worth reading: what a user is told to do, and what runs on install or import.
TEXT_SUFFIXES = (".md", ".txt", ".rst", ".py", ".ps1", ".bat", ".cmd", ".sh", ".js", ".mjs", ".ts",
                 ".json", ".toml", ".cfg", ".yaml", ".yml")
SKIP_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock")
SKIP_DIRS = (".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages", "dist", "build")
MAX_FILE_BYTES = 200_000
MAX_REMOTE_FILES = 14

# Link shorteners and file hosts a real project has no need to send its users to.
DOWNLOAD_HOSTS = ("mega.nz", "mediafire.com", "gofile.io", "anonfiles.com", "pixeldrain.com", "workupload.com",
                  "krakenfiles.com", "cdn.discordapp.com", "media.discordapp.net", "bit.ly", "tinyurl.com",
                  "rebrand.ly", "cutt.ly", "is.gd", "t.me", "telegra.ph", "pastebin.com", "rentry.co")
# Hosts a base64-hidden link may point at without being a sign of anything.
PLAIN_HOSTS = ("github.com", "githubusercontent.com", "huggingface.co", "pypi.org", "npmjs.com", "python.org",
               "w3.org", "schema.org", "json-schema.org", "mozilla.org", "apache.org", "opensource.org")
BINARY_ASSETS = (".exe", ".msi", ".scr", ".bat", ".cmd", ".zip", ".rar", ".7z", ".iso", ".dmg")

_B64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
_SHELL_WORDS = re.compile(r"powershell|cmd(\.exe)?\s*/c|invoke-webrequest|invoke-expression|\biex\b|"
                          r"curl\s|wget\s|certutil|bitsadmin|mshta|rundll32|start-process", re.I)
_AV_BYPASS = re.compile(
    r"(disabl\w+|turn(ing)?\s+off|switch(ing)?\s+off|deactivat\w+|paus\w+)\s+(your\s+|the\s+|windows\s+)*"
    r"(anti-?virus|defender|windows\s+security|real-?time\s+protection|smart\s?screen|firewall|av\b)"
    r"|add\s+(an?\s+)?exclusion|exclusion\s+(list|folder|path)|add-mppreference"
    r"|more\s+info\W+(then\s+)?(click\s+)?\W*run\s+anyway|run\s+anyway"
    r"|false\s+positive\w*[^.\n]{0,80}(ignore|disable|turn\s+off|allow|whitelist|exclu)"
    r"|(anti-?virus|defender)[^.\n]{0,60}(may|might|will|can)\s+(flag|block|detect|delete|quarantine)", re.I)
_ARCHIVE_PASSWORD = re.compile(r"(archive|zip|rar|7z|file)\s+password|pass(word)?\s*[:=]\s*`?\w{3,}`?", re.I)
_PS_ENCODED = re.compile(r"powershell(\.exe)?[^\n]{0,80}\s-(e|enc|encodedcommand)\s", re.I)
_EXEC_B64 = re.compile(r"(exec|eval)\s*\(\s*(compile\s*\()?[^)\n]{0,60}(b64decode|a85decode|fromhex|atob|unhexlify|decompress)"
                       r"|eval\s*\(\s*atob\s*\(|__import__\s*\(\s*['\"]base64['\"]\s*\)[^\n]{0,60}(exec|eval)", re.I)
_PIPE_SHELL = re.compile(r"(curl|wget|iwr|invoke-webrequest)\b[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z)?sh\b"
                         r"|(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]{0,200}\|\s*iex\b", re.I)
_IMPORT_FETCH = re.compile(r"(urlretrieve|requests\.get|httpx\.get|urlopen|download_url_to_file)\s*\([^)\n]*"
                           r"\.(exe|msi|bat|cmd|scr|ps1|zip|rar|7z)\b", re.I)
_REPO_URL = re.compile(r"(?:git\+)?(?:https?://|git@)github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
                       r"(?:\.git)?(?:(?:@|/archive/(?:refs/(?:tags|heads)/)?|/tree/|/commit/|/releases/download/)"
                       r"([A-Za-z0-9_./-]+?))?(?:\.tar\.gz|\.zip)?(?=$|[\s\"'#)\]>,;}`])", re.I)
_SHA = re.compile(r"[0-9a-f]{7,40}")
_TAG = re.compile(r"v?\d+(\.\d+){1,3}([.-]?\w+)?")


@dataclass(frozen=True)
class Signal:
    code: str          # short and stable: callers key on it
    level: str         # RED, CAUTION or INFO
    text: str          # one line for a person
    where: str = ""    # file or place, when there is one


@dataclass
class Report:
    repo: str
    ref: str = ""
    signals: List[Signal] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def verdict(self) -> str:
        return verdict(self.signals)

    def summary(self, limit: int = 8) -> str:
        shown = [s for s in self.signals if s.level != INFO] or self.signals
        shown = sorted(shown, key=lambda s: (RED, CAUTION, INFO).index(s.level))
        lines = [f"{self.repo}: {self.verdict}" + (f" (could not read everything: {self.error})" if self.error else "")]
        lines += [f"- {s.level}: {s.text}" + (f" ({s.where})" if s.where else "") for s in shown[:limit]]
        if len(shown) > limit:
            lines.append(f"- and {len(shown) - limit} more")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {"repo": self.repo, "ref": self.ref, "verdict": self.verdict, "error": self.error,
                "signals": [asdict(s) for s in self.signals], "facts": self.facts}


def verdict(signals: Iterable[Signal]) -> str:
    """Red on any red sign, or when three different cautions pile up; caution on any caution."""
    levels = Counter()
    for code, level in {(s.code, s.level) for s in signals}:
        levels[level] += 1
    if levels[RED] or levels[CAUTION] >= 3:
        return RED
    return CAUTION if levels[CAUTION] else "ok"


# --- repository references in text -----------------------------------------------------------

def parse_repo(url: str) -> Optional[Tuple[str, str]]:
    """``(owner/name, ref)`` from a GitHub URL in any of its shapes (``git+https://…@ref``,
    ``…/archive/refs/tags/v1.tar.gz``, ``…/tree/branch``, ``git@github.com:o/n.git``, ``o/n``)."""
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        return url.removesuffix(".git"), ""
    m = _REPO_URL.search(url)
    if not m:
        return None
    ref = (m.group(3) or "").rstrip("/")
    if "/releases/download/" in url and ref:
        ref = ref.split("/")[0]
    return f"{m.group(1)}/{m.group(2)}", ref


def repo_refs(text: str) -> List[Tuple[str, str]]:
    """Every GitHub repository a requirements file, script or Dockerfile points at, with its ref."""
    found: Dict[str, str] = {}
    for m in _REPO_URL.finditer(text):
        slug = f"{m.group(1)}/{m.group(2)}"
        if slug.lower().endswith((".md", ".png", ".jpg")) or m.group(1).lower() in ("orgs", "sponsors", "features"):
            continue
        ref = (m.group(3) or "").rstrip("/")
        if "/releases/download/" in m.group(0) and ref:
            ref = ref.split("/")[0]
        found[slug] = found.get(slug) or ref
    return sorted(found.items())


def is_pinned(ref: str) -> bool:
    """A commit or a version tag stays what it was; a branch (or no ref) is whatever is pushed next."""
    ref = ref.strip()
    return bool(ref) and bool(_SHA.fullmatch(ref) or _TAG.fullmatch(ref))


# --- the content checks -----------------------------------------------------------------------

def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split("@")[-1].split(":")[0].lower()


def _on(host: str, hosts: Sequence[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def decoded_base64(text: str) -> List[str]:
    """Readable strings hidden as base64 in ``text`` (hashes, keys and images decode to noise)."""
    out = []
    for token in _B64.findall(text):
        if len(token) % 4 and not token.endswith("="):
            token = token[: len(token) - len(token) % 4]
        try:
            raw = base64.b64decode(token, validate=True)
        except (binascii.Error, ValueError):
            continue
        for encoding in ("utf-8", "utf-16-le"):
            try:
                decoded = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in decoded)
            if decoded and printable / len(decoded) > 0.95:
                out.append(decoded)
                break
    return out


def scan_text(path: str, text: str) -> List[Signal]:
    """The signs one text file can show. ``path`` decides how much weight a line gets: an
    instruction in a README is addressed to the user, the same words in code may be a comment."""
    name = path.lower().rsplit("/", 1)[-1]
    doc = name.endswith((".md", ".txt", ".rst")) or name.startswith("readme")
    signals: List[Signal] = []

    def add(code: str, level: str, what: str) -> None:
        signals.append(Signal(code, level, what, path))

    for decoded in decoded_base64(text):
        urls = [u for u in _URL.findall(decoded) if not _on(_host(u), PLAIN_HOSTS)]
        if urls:
            add("hidden_link", RED, f"a base64 string hides a link to {_host(urls[0])}")
        elif _SHELL_WORDS.search(decoded):
            add("hidden_command", RED, f"a base64 string hides a shell command ({decoded.strip()[:50]!r})")
    if _PS_ENCODED.search(text):
        add("encoded_powershell", RED, "runs PowerShell with an encoded command")
    if not doc and _EXEC_B64.search(text):
        add("exec_decoded", RED, "executes code it decodes at run time")
    if not doc and _IMPORT_FETCH.search(text):
        add("fetches_binary", RED, "downloads an executable or archive from code")
    m = _AV_BYPASS.search(text)
    if m:
        add("av_bypass", RED if doc else CAUTION,
            f"tells the user to get past the antivirus or SmartScreen ({' '.join(m.group(0).split())[:60]!r})")
    hosts = sorted({_host(u) for u in _URL.findall(text) if _on(_host(u), DOWNLOAD_HOSTS)})
    if hosts:
        password = doc and _ARCHIVE_PASSWORD.search(text)
        add("download_host", RED if password else CAUTION,
            f"sends the user to {', '.join(hosts[:3])}" + (" for a password-protected archive" if password else ""))
    if _PIPE_SHELL.search(text):
        piped = [u for u in _URL.findall(_PIPE_SHELL.search(text).group(0))]
        if not piped or not _on(_host(piped[0]), PLAIN_HOSTS + ("astral.sh", "rustup.rs", "sh.rustup.rs", "bun.sh",
                                                               "deno.land", "get.docker.com", "ollama.com", "brew.sh")):
            # In a README it is how half of all tools say "install me"; in a script it is what runs.
            add("pipe_to_shell", INFO if doc else CAUTION, "pipes a download straight into a shell")
    return signals


def commit_signals(commits: Sequence[Dict[str, Any]]) -> List[Signal]:
    """Automated, meaningless activity. ``commits``: newest first, each ``{date, author, message,
    files}`` (``files``: the paths it changed, or None when not fetched)."""
    if len(commits) < 20:
        return []
    signals: List[Signal] = []
    messages = Counter(" ".join(str(c.get("message") or "").split("\n")[0].split()).lower() for c in commits)
    top_message, top_n = messages.most_common(1)[0]
    authors = {str(c.get("author") or "") for c in commits}
    detailed = [c for c in commits if c.get("files") is not None]
    one_file = Counter(c["files"][0] for c in detailed if len(c["files"]) == 1)
    same_file, same_n = one_file.most_common(1)[0] if one_file else ("", 0)
    repeated = top_n / len(commits) >= 0.6
    churn = bool(detailed) and same_n / len(detailed) >= 0.7
    days = Counter(time.strftime("%Y-%m-%d", time.gmtime(c["date"])) for c in commits if c.get("date"))
    if churn and (repeated or len(authors) == 1):
        signals.append(Signal("fake_activity", CAUTION,
                              f"{same_n} of {len(detailed)} recent commits only touch {same_file}"
                              + (f", {top_n} of {len(commits)} say {top_message!r}" if repeated else "")))
    elif repeated and len(authors) == 1 and len(days) >= 5:
        signals.append(Signal("fake_activity", CAUTION,
                              f"{top_n} of the last {len(commits)} commits have the same message {top_message!r}"))
    return signals


def substance_signals(*, readme_bytes: int, code_bytes: int, code_files: int,
                      release_assets: Sequence[str] = ()) -> List[Signal]:
    """Big claims, little code: a long README over almost no source, or releases that ship
    executables the source in the repository could not have built."""
    signals: List[Signal] = []
    binaries = sorted({a for a in release_assets if a.lower().endswith(BINARY_ASSETS)})
    if readme_bytes >= 3000 and (code_bytes < 15_000 or code_files <= 2) and readme_bytes > code_bytes / 3:
        signals.append(Signal("thin_code", CAUTION,
                              f"a {readme_bytes // 1000} kB README over {code_bytes // 1000} kB of code in "
                              f"{code_files} file{'s' if code_files != 1 else ''}"))
    if binaries and code_bytes < 50_000:
        signals.append(Signal("binary_release", CAUTION,
                              f"releases ship {', '.join(binaries[:3])} with only {code_bytes // 1000} kB of source"))
    return signals


def identity_signals(meta: Dict[str, Any], owner: Dict[str, Any], twins: Sequence[Dict[str, Any]],
                     star_times: Sequence[float], now: float) -> List[Signal]:
    """Who this is: a young repository or account, a copy of a much bigger project with the
    same name, stars that all arrived at once."""
    signals: List[Signal] = []
    stars = int(meta.get("stargazers_count") or 0)
    age_days = (now - _ts(meta.get("created_at"))) / 86400
    owner_days = (now - _ts(owner.get("created_at"))) / 86400 if owner.get("created_at") else None
    parent = (meta.get("parent") or {}).get("full_name") or ""
    if age_days < 30:
        signals.append(Signal("new_repo", CAUTION, f"the repository is {int(age_days)} days old"))
    if owner_days is not None and owner_days < 30:
        signals.append(Signal("new_owner", CAUTION, f"its owner's account is {int(owner_days)} days old"))
    mine = str(meta.get("full_name") or "").lower()
    for twin in twins:
        other = str(twin.get("full_name") or "")
        if other.lower() == mine or other.split("/")[-1].lower() != mine.split("/")[-1]:
            continue
        if int(twin.get("stargazers_count") or 0) >= max(50, 10 * stars):
            if parent.lower() == other.lower():
                signals.append(Signal("fork", INFO, f"a fork of {other} ({twin['stargazers_count']} stars)"))
            else:
                signals.append(Signal("lookalike", CAUTION,
                                      f"same name as {other} ({twin['stargazers_count']} stars) but not a fork of it"))
            break
    if stars >= 50 and len(star_times) >= 50:
        ordered = sorted(star_times)
        window = ordered[int(len(ordered) * 0.8) - 1] - ordered[0]
        if window <= 48 * 3600 and age_days > 7:
            signals.append(Signal("star_burst", CAUTION, f"80% of its first {len(ordered)} stars came within two days"))
    return signals


def _ts(value: Any) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


# --- a folder on disk -------------------------------------------------------------------------

def scan_tree(root: Path, max_files: int = 400) -> List[Signal]:
    """The content checks on a clone that is already on disk. Reads text files only; nothing
    is imported or run."""
    signals: List[Signal] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if not filename.lower().endswith(TEXT_SUFFIXES) or filename in SKIP_NAMES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            signals.extend(scan_text(str(path.relative_to(root)), text))
            seen += 1
            if seen >= max_files:
                return signals
    return signals


# --- GitHub -----------------------------------------------------------------------------------

Get = Callable[..., Any]     # get(path, accept=None, raw=False) -> parsed JSON, or text when raw


def github_get(token: Optional[str] = None, client: Optional[httpx.Client] = None) -> Get:
    """A reader for api.github.com. The token (``GH_TOKEN`` / ``GITHUB_TOKEN``) only lifts the
    rate limit from 60 to 5000 requests an hour; public repositories need none."""
    token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    http = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def get(path: str, accept: Optional[str] = None, raw: bool = False) -> Any:
        headers = {"Accept": accept or "application/vnd.github+json", "User-Agent": "labkit-repovet"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = http.get(path if path.startswith("http") else API + path, headers=headers)
        response.raise_for_status()
        return response.text if raw else response.json()

    return get


def _wanted(paths: Sequence[str]) -> List[str]:
    """The few files worth reading remotely: what the user is told, and what runs first."""
    def rank(path: str) -> Tuple[int, int]:
        low = path.lower()
        name = low.rsplit("/", 1)[-1]
        depth = low.count("/")
        if name.startswith("readme"):
            return (0, depth)
        if name in ("install.py", "setup.py", "__init__.py", "install.bat", "install.ps1", "install.sh",
                    "run.bat", "start.bat", "prestartup_script.py", "postinstall.js", "package.json"):
            return (1, depth)
        if name.endswith((".bat", ".cmd", ".ps1", ".sh")):
            return (2, depth)
        if name.endswith(".md"):
            return (3, depth)
        return (4, depth)

    keep = [p for p in paths if p.lower().endswith(TEXT_SUFFIXES) and p.rsplit("/", 1)[-1] not in SKIP_NAMES
            and not any(part in SKIP_DIRS for part in p.split("/"))]
    return sorted(keep, key=rank)[:MAX_REMOTE_FILES]


def vet(url: str, ref: Optional[str] = None, *, get: Optional[Get] = None, now: Optional[float] = None,
        commit_details: int = 12) -> Report:
    """Vet one GitHub repository (at ``ref``, default its default branch). Never raises for a
    repository it cannot read: the report then carries ``error`` and what it did see."""
    parsed = parse_repo(url)
    if not parsed:
        return Report(repo=url, error="not a GitHub repository URL")
    repo, url_ref = parsed
    ref = ref or url_ref
    get = get or github_get()
    now = now or time.time()
    report = Report(repo=repo, ref=ref)
    try:
        meta = get(f"/repos/{repo}")
    except Exception as exc:  # noqa: BLE001 - one report per repository, whatever went wrong
        report.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        return report
    report.repo = meta.get("full_name") or repo
    at = ref or meta.get("default_branch") or "HEAD"

    def step(name: str, call: Callable[[], Any], default: Any, *, optional: bool = False) -> Any:
        """One read, tried twice (raw.githubusercontent.com answers the odd 5xx or 429). A piece
        that stays missing is named in ``report.error`` unless it is ``optional``; the rest counts."""
        for attempt in (1, 2):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - one report per repository, whatever went wrong
                if attempt == 2 and not optional:
                    report.error = (report.error + "; " if report.error else "") + f"{name}: {type(exc).__name__}"
        return default

    owner = step("owner", lambda: get(f"/users/{meta['owner']['login']}"), {})
    languages = step("languages", lambda: get(f"/repos/{repo}/languages"), {})
    tree = step("tree", lambda: get(f"/repos/{repo}/git/trees/{at}?recursive=1"), {}).get("tree") or []
    releases = step("releases", lambda: get(f"/repos/{repo}/releases?per_page=5"), [])
    listed = step("commits", lambda: get(f"/repos/{repo}/commits?sha={at}&per_page=60"), [])
    name = report.repo.split("/")[-1]
    twins = step("search", lambda: get(f"/search/repositories?q={name}+in:name&sort=stars&per_page=5"), {}).get("items") or []
    # Optional: a fine-grained token may not list another account's stargazers (403); the
    # star-burst check then simply does not run.
    stars = step("stars", lambda: get(f"/repos/{repo}/stargazers?per_page=100",
                                      accept="application/vnd.github.star+json"), [], optional=True) \
        if int(meta.get("stargazers_count") or 0) >= 50 else []

    commits = []
    for index, row in enumerate(listed):
        info = row.get("commit") or {}
        files = None
        if index < commit_details and len(listed) >= 20:
            detail = step("commit", lambda sha=row["sha"]: get(f"/repos/{repo}/commits/{sha}"), None)
            files = [f["filename"] for f in (detail or {}).get("files") or []] if detail else None
        commits.append({"date": _ts((info.get("committer") or {}).get("date")),
                        "author": (info.get("author") or {}).get("name") or "",
                        "message": info.get("message") or "", "files": files})

    blobs = [item for item in tree if item.get("type") == "blob"]
    paths = [item["path"] for item in blobs]
    code_suffixes = (".py", ".js", ".ts", ".mjs", ".rs", ".go", ".c", ".cc", ".cpp", ".h", ".java", ".cs", ".sh")
    code_files = sum(1 for p in paths if p.lower().endswith(code_suffixes))
    code_bytes = sum(size for lang, size in languages.items() if lang not in ("HTML", "CSS", "Jupyter Notebook"))
    readme = next((item for item in blobs if item["path"].lower().startswith("readme")), None)
    assets = [asset["name"] for release in releases for asset in release.get("assets") or []]

    signals = identity_signals(meta, owner, twins, [_ts(s.get("starred_at")) for s in stars if s.get("starred_at")], now)
    signals += commit_signals(commits)
    signals += substance_signals(readme_bytes=int((readme or {}).get("size") or 0), code_bytes=code_bytes,
                                 code_files=code_files, release_assets=assets)
    sizes = {item["path"]: int(item.get("size") or 0) for item in blobs}
    for path in _wanted(paths):
        if sizes.get(path, 0) > MAX_FILE_BYTES:
            continue
        text = step("file", lambda p=path: get(f"https://raw.githubusercontent.com/{report.repo}/{at}/{p}", raw=True), "")
        signals += scan_text(path, text)
    for release in releases[:3]:
        signals += scan_text("release notes", str(release.get("body") or ""))
    if ref and not is_pinned(ref):
        signals.append(Signal("unpinned", INFO, f"used at the branch {ref!r}, not at a commit or tag"))

    seen = set()
    report.signals = [s for s in signals if not ((s.code, s.where) in seen or seen.add((s.code, s.where)))]
    report.facts = {"stars": meta.get("stargazers_count"), "forks": meta.get("forks_count"),
                    "created": meta.get("created_at"), "pushed": meta.get("pushed_at"),
                    "fork_of": (meta.get("parent") or {}).get("full_name") or "",
                    "code_kb": code_bytes // 1000, "code_files": code_files, "commits_read": len(commits),
                    "files_read": len(_wanted(paths)), "archived": bool(meta.get("archived"))}
    return report


# --- CLI --------------------------------------------------------------------------------------

def add_parser(sub) -> None:
    p = sub.add_parser("vet", help="check a GitHub repository for the signs of a fake project",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="a GitHub URL or owner/name")
    p.add_argument("--ref", help="branch, tag or commit to read (default: the default branch)")
    p.add_argument("--json", action="store_true", help="print the whole report as JSON")
    p.set_defaults(func=_main)


def _main(a: argparse.Namespace) -> int:
    report = vet(a.url, a.ref)
    print(json.dumps(report.to_dict(), indent=1) if a.json else report.summary(limit=20))
    return {"ok": 0, "caution": 1, "red": 2}[report.verdict] if not report.error else 3
