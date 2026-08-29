"""Self-update for NyaaTriggers.

Pure logic, no Qt. The GUI runs the network/IO on a worker thread.

Install kinds, auto-detected.
  - "git"            - source checkout -> `git pull --ff-only --tags`, then
                       `pip install -r requirements.txt` when the pull moved
                       HEAD, since new code can need new or changed pinned deps.
  - "frozen-linux"   - PyInstaller ONEDIR -> download NyaaTriggers-linux.tar.gz,
                       swap exe + _internal/ in place, keep user-data siblings.
  - "frozen-windows" - ONEDIR. The running exe + DLLs are locked, so a staged
                       copy finishes the swap after this process exits
                       via apply_frozen_windows -> finish_windows_update, confirms
                       the new build boots, and rolls back if not.
  - "source"         - non-git copy -> manual, open the releases page.

Frozen layout, PyInstaller 6.x ONEDIR.
    <install_dir>/
        NyaaTriggers            <- launcher exe, same as Path sys.executable
        _internal/              <- all libs + bundled data
        nyaatriggers_settings.json, triggers.local.json, timelines/, ...  <- user data
The release tarball wraps everything in one top-level "NyaaTriggers/" folder.
User data is never in the archive, so only the exe and _internal/ are replaced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO            = "CateDesu/NyaaTriggers"
API_LATEST_URL  = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL    = f"https://github.com/{REPO}/releases/latest"
LINUX_ASSET     = "NyaaTriggers-linux.tar.gz"
WINDOWS_ASSET   = "NyaaTriggers-windows.zip"
_USER_AGENT     = "NyaaTriggers"
_BACKUP_SUFFIX  = ".nyaa-old"     # marks files left behind for next-launch cleanup
# Written by a normal launch once the app has booted, see mark_boot_ok. The
# Windows staged updater waits for it to confirm a swapped install launches.
_BOOT_OK_MARKER = ".nyaa-boot-ok"
# finish_windows_update evidence, both in the install dir. One line per exit
# appended to the log, and a boot-verify rollback also drops a sentinel naming
# the rejected build. Best effort only. Those paths must never raise.
_UPDATE_LOG_NAME = "nyaatriggers-update.log"
_REJECTED_NAME   = ".nyaa-update-rejected"
# Staging dirs apply_frozen_* create in the install dir's parent. The Windows
# --apply-update hand-off validates against it and the next-launch sweep globs it.
_STAGING_PREFIX  = ".nyaa-update-"
# Hard ceiling for one release download, applied whether or not the server sent
# an honest Content-Length. Real archives, JRE + jar + .NET, are a few hundred
# MB, so only an endless or lying stream ever trips this.
_MAX_DOWNLOAD_BYTES = 2 * 1024**3
# Cap on the release API body. The payload is a few KB of JSON and the socket
# timeout is per read, not total, so an unbounded read lets a trickling peer
# grow memory without limit. The cap only bounds memory. fetch_latest_release
# also reads under a total deadline so a trickle can't hold the check open.
_MAX_RELEASE_BYTES = 4 << 20
# Watchdog timing for the two network reads below. Each read loop runs on a
# daemon helper thread while the calling thread enforces a stall window and
# a total deadline from outside the read, the same guard main.py runs for
# its downloads. The socket timeout is per recv and resets on every received
# byte, so it alone can never cut off a peer that trickles one byte at a time.
_READ_STALL_S = 60
_RELEASE_DEADLINE_S = 30
_DOWNLOAD_DEADLINE_S = 3600
# Last successful release lookup, kept in the install dir. The anonymous
# GitHub API allows 60 requests per hour per IP, and a shared NAT or VPN can
# burn through that. The cache lets a rate limited launch still see an update
# the last check already knew about.
_RELEASE_CACHE_NAME = "latest_release.json"


def _unblock_reader(resp) -> None:
    """Shut the underlying socket down so a read parked in another thread
    wakes at once. A plain resp.close from this side would block on the
    buffer lock the parked read still holds. Best effort, the reader is a
    daemon thread either way."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


class RateLimited(Exception):
    """GitHub answered 403 with a rate limit hit, the anonymous rate budget
    exhausted or a secondary limit asking for a Retry-After."""


@dataclass
class Release:
    tag: str
    version: str                         # tag with a leading "v" stripped
    html_url: str
    body: str = ""
    assets: dict[str, str] = field(default_factory=dict)   # name -> download url


# ── Version comparison ────────────────────────────────────────────────────

def _raw_segments(s: str) -> list[str]:
    """The dot-separated segments of a version string, unparsed, "v1.2.0" ->
    ["1", "2", "0"]. Used where the count must come from the raw string, not
    the parsed tuple, which has its trailing zeros stripped."""
    s = s.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    return s.split(".")


def _strip_v(tag: str) -> str:
    """The tag with exactly one leading v/V removed, "v1.2" -> "1.2" and
    "vv1.2" -> "v1.2". str.lstrip would eat every leading v and V."""
    return tag[1:] if tag[:1] in ("v", "V") else tag


def parse_version(s: str) -> tuple[int, ...]:
    """"v0.3.1" -> 0, 3, 1 as a tuple. Each segment contributes only its leading
    digits, so "0.4-rc1" -> 0, 4, not 0, 41. No leading digit means 0. Trailing
    zero segments are dropped so "1.0.0" == "1.0" instead of comparing newer."""
    out: list[int] = []
    for seg in _raw_segments(s):
        digits = ""
        for ch in seg:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out) or (0,)


def _has_suffix(s: str) -> bool:
    """True when any segment of the version string carries non-digit chars,
    like "1.2.3-rc1" or "0.4a1". A plain numeric version is the final."""
    return any(not seg.isdigit() for seg in _raw_segments(s))


def is_newer(remote: str, current: str) -> bool:
    """True if the remote version string is strictly newer than current. On a
    parsed-tuple tie a suffixed current loses to a plain remote: "1.2.3-rc1"
    parses to 1, 2, 3 like the final, and the rc user should still be offered
    the final of the same version."""
    r, c = parse_version(remote), parse_version(current)
    if r != c:
        return r > c
    return _has_suffix(current) and not _has_suffix(remote)


def is_update_for_here(remote: str, current: str, kind: str | None = None) -> bool:
    """is_newer, install-kind aware. Every kind now compares the full tag.
    Frozen builds carry the stamped rolling version, so strict is exact. Git
    and source installs report the base version, repo _VERSION is never
    run-number-stamped, so a rolling tag on their base compares newer and is
    offered. The UI snoozes each offered tag so the per-push release stream
    does not nag on every launch, and a git checkout's Install pulls it past
    the tag. `kind` is kept for backward compatibility and ignored."""
    return is_newer(remote, current)


# ── Install-kind detection ────────────────────────────────────────────────

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_dir() -> Path:
    """Directory of the source checkout, only meaningful when not frozen."""
    return Path(__file__).resolve().parent


def install_kind() -> str:
    if is_frozen():
        if sys.platform.startswith("win"):
            return "frozen-windows"
        return "frozen-linux"
    if (source_dir() / ".git").is_dir():
        return "git"
    return "source"


def can_self_apply(kind: str | None = None) -> bool:
    """Whether this install can apply an update itself vs just opening the page."""
    return (kind or install_kind()) in ("git", "frozen-linux", "frozen-windows")


def _describe_label(base: str, out: str) -> str | None:
    """Parse `git describe --tags` output into a display label, or None when
    it is not a same-base rolling tag. Same base line only. A tag from an
    older base, v1.2.7.x when base is 1.3.0, says nothing useful about this
    tree. Commits past the tag are not shown, the label stays the plain tag
    version."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-(\d+)-g[0-9a-f]+)?", out.strip())
    if m and (m.group(1) == base or m.group(1).startswith(base + ".")):
        return m.group(1)
    return None


def display_version(base: str, kind: str | None = None) -> str:
    """The version string the UI should show where a screenshot can see it.
    Frozen builds have the full rolling stamp baked into _VERSION at build
    time, so base is already complete. A git checkout derives its rolling
    identity from the nearest rolling tag, v1.3.0.165-9-gdeadbee shows as
    "1.3.0.165". A plain source copy has no rolling identity at all. "-src"
    marks it so it is not mistaken for a release build. Update logic keeps
    using plain _VERSION. This is display only and must never break startup,
    so every failure returns base."""
    kind = kind or install_kind()
    if kind == "source":
        return f"{base}-src"
    if kind == "git":
        try:
            r = subprocess.run(
                ["git", "-C", str(source_dir()), "describe", "--tags",
                 "--match", "v[0-9]*"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - no git binary / repo gone
            return base
        if r.returncode == 0:
            label = _describe_label(base, r.stdout)
            if label:
                return label
    return base


def install_dir() -> Path:
    """The directory to swap app files in when frozen, or pull when source."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return source_dir()


def mark_boot_ok() -> None:
    """Drop a marker signalling this build booted. Called after QApplication is
    up, so _internal + the Qt platform plugin loaded. The Windows self-update
    waits for it after a swap and rolls back if it never appears, say antivirus
    quarantined a fresh DLL. Best effort, frozen only."""
    if not is_frozen():
        return
    try:
        (install_dir() / _BOOT_OK_MARKER).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


# ── Release lookup ────────────────────────────────────────────────────────

def _parse_release(data: dict) -> Release:
    tag = data.get("tag_name", "") or ""
    assets = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in data.get("assets", [])
        if a.get("name") and a.get("browser_download_url")
    }
    return Release(
        tag=tag,
        version=_strip_v(tag),
        html_url=data.get("html_url", "") or RELEASES_URL,
        body=data.get("body", "") or "",
        assets=assets,
    )


def fetch_latest_release(timeout: int = 8, channel: str = "stable") -> Release:
    """Latest release. Raises on network/parse error, RateLimited when GitHub
    reports the anonymous rate budget exhausted.

    The project has a single stable channel now. This always queries
    /releases/latest, which GitHub defines to exclude pre-releases and
    drafts. The `channel` parameter is kept for backward compatibility and
    ignored. A successful fetch is cached on disk for read_cached_release."""
    req = urllib.request.Request(API_LATEST_URL, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Read loop on a daemon helper, watchdog here. The body is a few
            # KB of JSON, so half a minute is already generous. The deadline
            # is enforced from outside the read because a trickling peer can
            # hold one resp.read open forever.
            done = threading.Event()
            progress = [0]
            reader_error = [None]
            chunks: list[bytes] = []

            def _reader() -> None:
                try:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        progress[0] += len(chunk)
                        if len(chunk) < 65536:
                            # urllib's read only returns short at the end of the body.
                            break
                        if progress[0] > _MAX_RELEASE_BYTES:
                            raise OSError(
                                f"Release info exceeded the {_MAX_RELEASE_BYTES >> 20} MB safety cap")
                except BaseException as exc:
                    reader_error[0] = exc
                finally:
                    done.set()

            threading.Thread(target=_reader, daemon=True).start()
            deadline = time.monotonic() + _RELEASE_DEADLINE_S
            last_seen = progress[0]
            while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                if progress[0] == last_seen or time.monotonic() > deadline:
                    # Shut the connection down so the parked reader wakes
                    # instead of leaking. A plain resp.close here would
                    # block on the lock the parked read still holds.
                    _unblock_reader(resp)
                    raise OSError("Release info timed out after 30 seconds.")
                last_seen = progress[0]
            if reader_error[0]:
                raise reader_error[0]
            body = b"".join(chunks)
        if len(body) > _MAX_RELEASE_BYTES:
            # Same treatment as the download byte cap. An oversized body is an
            # error in its own right. Truncating would only surface later as a
            # JSON parse failure with a misleading cause.
            raise OSError(
                f"Release info exceeded the {_MAX_RELEASE_BYTES >> 20} MB safety cap")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        # GitHub has two throttles. The anonymous budget exhausted sends
        # X-RateLimit-Remaining: 0. A secondary limit answers 403 with
        # Retry-After and can still show budget left. Both want the same
        # handling: back off and serve the cached release.
        # The error doubles as the response object. Close it so the
        # connection is not held open while the caller handles the failure.
        exc.close()
        if exc.code == 403 and (exc.headers.get("X-RateLimit-Remaining") == "0"
                                or "Retry-After" in exc.headers):
            raise RateLimited("GitHub API rate limit exhausted") from exc
        raise
    if not isinstance(data, dict) or not data.get("tag_name"):
        msg = data.get("message") if isinstance(data, dict) else None
        raise ValueError(f"GitHub API error: {msg}" if msg
                         else "unexpected API response")
    release = _parse_release(data)
    _write_release_cache(release)
    return release


def _release_cache_path() -> Path:
    return install_dir() / _RELEASE_CACHE_NAME


def _write_release_cache(release: Release) -> None:
    """Persist the last good release lookup. Best effort, a lost cache only
    means the rate-limit fallback has nothing to offer."""
    try:
        _release_cache_path().write_text(json.dumps({
            "tag":      release.tag,
            "version":  release.version,
            "html_url": release.html_url,
            "body":     release.body,
            "assets":   release.assets,
        }), encoding="utf-8")
    except OSError:
        pass


def read_cached_release() -> Release | None:
    """The last successfully fetched release, or None. Backs the update
    check's rate-limit fallback."""
    try:
        data = json.loads(_release_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tag"), str) or not data["tag"]:
        return None
    assets = data.get("assets")
    if not isinstance(assets, dict):
        assets = {}
    return Release(
        tag=data["tag"],
        version=str(data.get("version") or _strip_v(data["tag"])),
        html_url=str(data.get("html_url") or RELEASES_URL),
        body=str(data.get("body") or ""),
        assets={str(k): str(v) for k, v in assets.items()},
    )


def asset_for_platform(release: Release) -> str | None:
    """Download URL of the archive for this platform, or None."""
    kind = install_kind()
    if kind == "frozen-linux":
        return release.assets.get(LINUX_ASSET)
    if kind == "frozen-windows":
        return release.assets.get(WINDOWS_ASSET)
    return None


# ── Download ──────────────────────────────────────────────────────────────

def download(url: str, dest: Path, progress_cb: Callable[[int, int], None] | None = None, timeout: int = 60) -> None:
    """Stream url -> dest. Calls progress_cb with downloaded and total if given.
    Total is 0 with no Content-Length. Writes to a .part and renames on success
    so a half-download is never mistaken for complete. .part removed on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # Unique per-call .part name so two concurrent downloads of the same dest,
    # two app instances or a retried UI path, can't truncate each other's write.
    part = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # A junk length from a proxy reads as unknown, same as http.client
            # itself treats it. The hard byte cap below still bounds the read.
            try:
                total = int(resp.headers.get("Content-Length", 0) or 0)
            except ValueError:
                total = 0
            if total:
                free = shutil.disk_usage(dest.parent).free
                if free < total + (32 << 20):   # keep ~32 MB headroom
                    raise OSError(
                        f"Not enough free space to download the update: need "
                        f"~{total >> 20} MB, have {free >> 20} MB free.")
            # Read loop on a daemon helper, watchdog here. Bytes must keep
            # advancing within the stall window and the whole transfer must
            # fit the total deadline, both enforced from outside the read.
            # Same guard main.py runs for its downloads.
            done = threading.Event()
            progress = [0]
            reader_error = [None]

            def _reader() -> None:
                try:
                    with part.open("wb") as f:
                        while True:
                            chunk = resp.read(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            progress[0] += len(chunk)
                            if progress[0] > _MAX_DOWNLOAD_BYTES:
                                raise OSError(
                                    f"Download exceeded the {_MAX_DOWNLOAD_BYTES >> 30} GB "
                                    "safety cap (missing or lying Content-Length)")
                            if progress_cb:
                                progress_cb(progress[0], total)
                except BaseException as exc:
                    reader_error[0] = exc
                finally:
                    done.set()

            threading.Thread(target=_reader, daemon=True).start()
            deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
            last_seen = progress[0]
            last_change = time.monotonic()
            while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                now = time.monotonic()
                if progress[0] == last_seen or now > deadline:
                    # Shut the connection down so the parked reader wakes
                    # instead of leaking. A plain resp.close here would
                    # block on the lock the parked read still holds.
                    _unblock_reader(resp)
                    # Say which bound cut the read off. The stall line
                    # only fits when the whole stall window really
                    # passed with no byte. Near the deadline the wait is
                    # clamped short, so a wake there after less than a
                    # full window of quiet is the deadline, not a stall.
                    if now - last_change >= _READ_STALL_S:
                        raise OSError(
                            f"Download stalled, no new bytes for {_READ_STALL_S} seconds.")
                    raise OSError("Download timed out after 60 minutes.")
                last_seen = progress[0]
                last_change = now
            if reader_error[0]:
                raise reader_error[0]
        # A clean early connection close is a short read with no exception.
        if total and progress[0] < total:
            raise OSError(
                f"Download incomplete: received {progress[0]} of {total} bytes")
        os.replace(part, dest)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def verify_release_asset(release: Release, asset_name: str, archive: Path,
                         timeout: int = 15) -> tuple[bool, str]:
    """Verify a downloaded archive against the '<asset>.sha256' sidecar asset
    published with the release. Fails closed on every path, missing sidecar,
    unreadable sidecar, fetch error, or mismatch, because the archive is
    about to be executed and every release build publishes a sidecar."""
    url = release.assets.get(asset_name + ".sha256")
    if not url:
        return False, "no checksum published for this release"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(4096).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"could not fetch the published checksum: {exc}"
    m = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if not m:
        return False, "the published checksum file is unreadable"
    expected = m.group(0).lower()
    h = hashlib.sha256()
    with archive.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        return False, "SHA-256 checksum mismatch (corrupted or tampered download)"
    return True, "verified"


# ── Apply - git ────────────────────────────────────────────────────────────

def _git_head(repo_dir: Path) -> str | None:
    """Current HEAD commit hash, or None when unreadable."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_covers_upstream(repo_dir: Path | None = None, timeout: int = 10) -> bool:
    """True when this checkout's HEAD already contains the upstream branch
    tip, so a rolling release built from that tip has nothing new to offer.
    Every push to main builds such a tag and it always sorts above the
    checkout's base _VERSION, which is how a maintainer gets offered their
    own push. Any uncertainty, no git, no upstream, offline, a tip object the
    clone has not fetched yet, answers False and the caller falls back to the
    plain version comparison."""
    repo = str(repo_dir or source_dir())

    def _git(*args: str) -> "subprocess.CompletedProcess | None":
        try:
            r = subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except Exception:  # noqa: BLE001 - no git binary, no network, timed out
            return None
        return r if r.returncode == 0 else None

    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if up is None:
        return False
    remote, _, branch = up.stdout.strip().partition("/")
    if not remote or not branch:
        return False
    tip = _git("ls-remote", remote, f"refs/heads/{branch}")
    if tip is None or not tip.stdout.split():
        return False
    sha = tip.stdout.split()[0]
    # Equal to or an ancestor of HEAD means nothing new upstream. A tip the
    # clone has not fetched errors out of the ancestor test and reads False.
    return _git("merge-base", "--is-ancestor", sha, "HEAD") is not None


def _install_requirements(repo_dir: Path) -> tuple[bool, str] | None:
    """`pip install -r requirements.txt` into the interpreter running the app.

    A pull brings new code but never the pinned packages that code needs. The
    plugin link's websockets was one such case. Envs that predated its
    requirements.txt entry stayed broken through every update. Runs after any
    pull that moved HEAD, not just ones touching requirements.txt, so an env
    that already missed a dep heals on the next update. Already-satisfied pins
    download nothing, so this is cheap. Returns None when there is no
    requirements.txt to install from, else ok and detail."""
    req = repo_dir / "requirements.txt"
    if not req.is_file():
        return None
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--disable-pip-version-check", "-r", str(req)],
            capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr.strip() or r.stdout.strip() or "unknown error")


def apply_git(repo_dir: Path | None = None) -> tuple[bool, str]:
    """`git pull --ff-only --tags` in the source checkout, then refresh the
    pip requirements when the pull moved HEAD. Tags ride along so the git
    describe version label catches up to the rolling tags each push to main
    is cut from: plain tag following only fetches tags for commits the pull
    downloads, and a maintainer downloads none of their own. A pip failure
    never fails the update itself. The message asks for a manual install
    instead. Returns ok and a message."""
    repo_dir = repo_dir or source_dir()
    head_before = _git_head(repo_dir)
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only", "--tags"],
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return False, "git is not installed or not on PATH."
    except Exception as exc:  # noqa: BLE001
        return False, f"git pull failed: {exc}"
    if r.returncode == 0:
        msg = r.stdout.strip() or "Updated."
        if _git_head(repo_dir) != head_before:
            deps = _install_requirements(repo_dir)
            if deps is not None:
                deps_ok, detail = deps
                if deps_ok:
                    msg += "\n\nPython dependencies are up to date."
                else:
                    msg += ("\n\nThe update applied, but installing the Python "
                            f"dependencies failed:\n{detail}\n"
                            "Run `pip install -r requirements.txt` yourself, then "
                            "restart the app.")
        return True, msg
    detail = (r.stderr.strip() or r.stdout.strip() or "unknown error")
    return False, (
        f"git pull failed:\n{detail}\n\n"
        "If you have local edits to tracked files, stash or revert them and try "
        "again, or update manually."
    )


# ── Apply - frozen ONEDIR on Linux ─────────────────────────────────────────

def _archive_app_root(extracted_to: Path) -> Path:
    """Folder holding the new exe + _internal inside an extracted archive.
    Usually the top-level "NyaaTriggers/". Falls back to the extract dir or
    whichever subdir holds _internal."""
    if (extracted_to / "_internal").is_dir():
        return extracted_to
    nested = extracted_to / "NyaaTriggers"
    if (nested / "_internal").is_dir():
        return nested
    for child in sorted(extracted_to.iterdir()):
        if child.is_dir() and (child / "_internal").is_dir():
            return child
    return nested


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract a tar.gz, rejecting any member that would escape dest.

    Guards ../ traversal and symlink/hardlink targets outside dest. Links that
    stay inside dest must be allowed. The PyInstaller Linux build legitimately
    has relative symlinks in _internal, libQt6Core.so.6 for one, and a blanket
    link ban broke every real update."""
    dest = dest.resolve()
    dest_s = str(dest)

    def _inside(p: Path) -> bool:
        ps = str(p)
        return ps == dest_s or ps.startswith(dest_s + os.sep)

    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            # Nothing extracted yet, so resolve only normalises lexically.
            target = (dest / member.name).resolve()
            if not _inside(target):
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym():
                # symlink target is relative to the link's own directory.
                link_target = (target.parent / member.linkname).resolve()
                if not _inside(link_target):
                    raise RuntimeError(
                        f"unsafe symlink target in archive: {member.name} -> {member.linkname}")
            elif member.islnk():
                # hardlink target is a path relative to the archive root.
                link_target = (dest / member.linkname).resolve()
                if not _inside(link_target):
                    raise RuntimeError(
                        f"unsafe hardlink target in archive: {member.name} -> {member.linkname}")
        # The lexical checks above run before extraction, so a symlink member can
        # be created and then a later member escapes through it. filter="data",
        # 3.11.4 and up, re-resolves every member at extraction time and blocks
        # that, while still allowing the build's in-tree relative symlinks.
        tf.extractall(dest, filter="data")


def apply_frozen_linux(tar_path: Path, dest_dir: Path | None = None,
                       exe_name: str | None = None) -> tuple[bool, str]:
    """Swap exe + _internal/ from a downloaded tarball into the install dir,
    preserving user-data siblings. Old files are renamed aside with the backup
    suffix and removed next launch. Returns ok and msg."""
    dest_dir = (dest_dir or install_dir()).resolve()
    exe_name = exe_name or Path(sys.executable).name
    if not os.access(dest_dir, os.W_OK):
        return False, f"No write permission for the install folder:\n{dest_dir}"

    staging = None
    try:
        # Inside the try. The install dir itself can be writable while its
        # PARENT is not, say a user-owned /opt/NyaaTriggers under a root-owned
        # /opt, and this function's contract is ok/msg, never an exception.
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(dest_dir.parent)))
        _safe_extract_tar(tar_path, staging)
        new_root = _archive_app_root(staging)
        new_exe = new_root / "NyaaTriggers"
        new_internal = new_root / "_internal"
        if not new_internal.is_dir() or not new_exe.exists():
            return False, "Downloaded update is missing expected files (exe / _internal)."

        os.chmod(new_exe, 0o755)
        # The launcher carries the pre-boot _internal recovery. Not swap
        # critical, the exe runs fine without it, so a copy failure must not
        # roll the update back. Archives from before it shipped just skip it.
        try:
            new_launcher = new_root / "NyaaTriggers.sh"
            if new_launcher.exists():
                launcher_dst = dest_dir / "NyaaTriggers.sh"
                # Temp file then rename, the .part protocol from download.
                # A kill mid copy leaves a stale temp, not a truncated
                # launcher.
                tmp = dest_dir / f"NyaaTriggers.sh.{os.getpid()}.{threading.get_ident()}.part"
                try:
                    shutil.copy2(str(new_launcher), str(tmp))
                    os.chmod(tmp, 0o755)
                    os.replace(str(tmp), str(launcher_dst))
                except OSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
        internal_dst = dest_dir / "_internal"
        exe_dst = dest_dir / exe_name
        # Pid-suffixed like the Windows backups. Two instances updating in the
        # same window no longer race over one fixed name, and the
        # _BACKUP_SUFFIX ending keeps them inside cleanup_old_backups' glob.
        pid = os.getpid()
        internal_backup = dest_dir / f"_internal.{pid}{_BACKUP_SUFFIX}"
        exe_backup = dest_dir / f"{exe_name}.{pid}{_BACKUP_SUFFIX}"
        internal_swapped = False
        try:
            # Step 1, _internal. A dir can't be atomically overwritten, so two
            # adjacent renames on the same fs. Open inodes keep the running process safe.
            if internal_dst.exists():
                if internal_backup.exists():
                    _force_remove(internal_backup)
                os.replace(internal_dst, internal_backup)
                internal_swapped = True
            # os.replace, not shutil.move. Staging sits in dest's parent so this
            # is a same-fs rename. If the install dir is its own mount, fail
            # fast with EXDEV instead of a silent non-atomic copy.
            os.replace(str(new_internal), str(internal_dst))

            # Step 2, the exe. Backup copy, then one atomic os.replace. The exe
            # is never absent, so a kill here can't leave an unlaunchable install.
            if exe_dst.exists():
                if exe_backup.exists():
                    _force_remove(exe_backup)
                shutil.copy2(exe_dst, exe_backup)
            os.replace(str(new_exe), str(exe_dst))   # same fs, staging sits in dest's parent
        except Exception:
            # Roll back the _internal swap if the exe step failed.
            if internal_swapped and internal_backup.exists():
                try:
                    _force_remove(internal_dst)
                    os.replace(internal_backup, internal_dst)
                except Exception:  # noqa: BLE001
                    pass
            raise
        return True, "Update installed."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not install the update: {exc}"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


# ── Apply - frozen ONEDIR on Windows ───────────────────────────────────────
# Windows locks the running exe + _internal/*.dll, so no in-place swap. The
# fresh copy installs itself. Extract to staging, launch the NEW exe with
# --apply-update, quit. That process waits for this one to exit, copies the
# files over, and relaunches. Any failure before hand-off returns False and a
# message with the on-disk install untouched.

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a .zip, rejecting path traversal. Mirrors _safe_extract_tar.
    The Windows build has no symlinks to worry about."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise RuntimeError(f"unsafe path in archive: {name}")
        zf.extractall(dest)


def _dir_writable(d: Path) -> bool:
    """True if we can actually create a file in d. os.access with W_OK is
    meaningless for directories on Windows, it ignores ACLs/UAC, so probe for real."""
    try:
        with tempfile.NamedTemporaryFile(dir=str(d), prefix=".nyaa-wtest-"):
            pass
        return True
    except OSError:
        return False


def apply_frozen_windows(zip_path: Path, dest_dir: Path | None = None,
                         exe_name: str | None = None) -> tuple[bool, str]:
    """Stage the new Windows build. The staged exe finishes the swap after this
    process exits. On success returns True and "__windows_handoff__", and the
    caller MUST quit promptly so the locked files release. On failure returns
    False and a message, with nothing on disk changed."""
    dest_dir = (dest_dir or install_dir()).resolve()
    exe_name = exe_name or Path(sys.executable).name
    if not _dir_writable(dest_dir):
        return False, f"No write permission for the install folder:\n{dest_dir}"

    staging = None
    try:
        # Preflight free space on the INSTALL volume. The downloader only checked
        # the temp volume. Staging + swap copy + backup needs ~3x unpacked + headroom.
        with zipfile.ZipFile(zip_path) as zf:
            unpacked = sum(i.file_size for i in zf.infolist())
        need = unpacked * 3 + (64 << 20)
        # Staging unpacks onto the parent volume while the swap copies land on
        # the dest volume. Usually the same disk, but dest_dir can be its own
        # mount point, so require headroom on BOTH.
        for volume in (dest_dir, dest_dir.parent):
            free = shutil.disk_usage(volume).free
            if free < need:
                return False, (f"Not enough free space on the install drive to update: "
                               f"need ~{need >> 20} MB, have {free >> 20} MB free.")

        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(dest_dir.parent)))
        _safe_extract_zip(zip_path, staging)
        new_root = _archive_app_root(staging)
        new_exe = new_root / exe_name
        if not (new_root / "_internal").is_dir() or not new_exe.exists():
            shutil.rmtree(staging, ignore_errors=True)
            return False, "Downloaded update is missing expected files (exe / _internal)."
        # Launch the staged exe to do the swap once we exit. DETACHED_PROCESS so
        # it outlives us. Do NOT also OR in CREATE_NO_WINDOW because CreateProcess
        # rejects the combination with WinError 87.
        cmd = [str(new_exe), "--apply-update",
               "--dest", str(dest_dir),
               "--staging", str(new_root),
               "--pid", str(os.getpid()),
               "--exe-name", exe_name]
        proc = subprocess.Popen(
            cmd, cwd=str(new_root), close_fds=True,
            creationflags=_DETACHED_PROCESS,
        )
        # Confirm the staged updater survived launch. An unsigned fresh exe is a
        # prime Defender quarantine target. Never quit into a dead hand-off. The
        # child is parked in _wait_for_pid_exit during this ~4s poll.
        for _ in range(20):
            time.sleep(0.2)
            rc = proc.poll()
            if rc is not None:
                shutil.rmtree(staging, ignore_errors=True)
                if rc == 0:
                    # Python ran and exited on its own, so the hand-off was
                    # refused, not blocked. finish_windows_update logs why.
                    return False, (f"The staged updater refused the update, see "
                                   f"{_UPDATE_LOG_NAME} next to the app for why. "
                                   "Please use the manual download.")
                return False, ("The staged updater was blocked from starting "
                               "(antivirus may have quarantined it). Please use the "
                               "manual download.")
        return True, "__windows_handoff__"
    except Exception as exc:  # noqa: BLE001
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        return False, f"Could not stage the update: {exc}"


def _wait_for_pid_exit(pid: int, timeout: float = 90.0) -> bool:
    """Block until process `pid` is gone, plus a grace sleep so the OS releases
    file locks. Prefers a Win32 wait handle, immune to PID reuse, falls back to
    a tasklist poll. Returns False on timeout. pid <= 0 counts as already gone."""
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    # The SYNCHRONIZE handle refers to the original process object. A reused PID
    # can't fool it.
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _SYNCHRONIZE = 0x00100000
        _WAIT_OBJECT_0 = 0x00000000
        handle = k32.OpenProcess(_SYNCHRONIZE, False, pid)
        if handle:
            try:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                res = k32.WaitForSingleObject(handle, remaining)
            finally:
                k32.CloseHandle(handle)
            if res == _WAIT_OBJECT_0:
                time.sleep(1.5)   # grace for handle and lock release
                return True
            return False          # WAIT_TIMEOUT or error, exit not confirmed
        # OpenProcess came back NULL. Usually that means the process already
        # exited, but every open failure looks the same here, so fall through
        # to the tasklist poll for the answer. A process that did exit reads
        # as gone there too.
    except Exception:  # noqa: BLE001 - kernel32/ctypes unavailable: fall back to tasklist
        pass
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:  # noqa: BLE001 - tasklist unavailable/slow: wait out a grace
            time.sleep(3.0)   # do NOT assume exit, just retry until the deadline
            continue
        out = (r.stdout or "").lstrip()
        # Exited ONLY on a successful probe saying so. That means empty stdout
        # on a clean run, or the "no tasks" INFO line. tasklist localizes that
        # line, so match the INFO prefix rather than the English text. French
        # writes it INFO : with a space before the colon, so allow both. A live
        # process prints a CSV-quoted row, which starts with a quote and never
        # with INFO. A nonzero exit, an ERROR line, or any other unexpected
        # output means the probe itself failed. Keep polling instead
        # of swapping files the old process may still hold open.
        if (not out and r.returncode == 0) or out.startswith(("INFO:", "INFO :")):
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return False


def _retry_locked(fn, attempts: int = 30, delay: float = 0.5):
    """Run fn, retrying on PermissionError from a lingering file lock. Re-raises
    the last error."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def _install_looks_intact(dest_dir: Path, exe_dst: Path) -> bool:
    """Cheap launchability check. exe exists and _internal/ is non-empty. Good
    enough because the rename swap never leaves _internal half-written."""
    try:
        internal = dest_dir / "_internal"
        if not exe_dst.exists() or not internal.is_dir():
            return False
        # any returns False on an empty dir, no StopIteration footgun under
        # PEP 479 if this is ever refactored into a generator helper.
        return any(internal.iterdir())
    except OSError:
        return False


def _relaunch_installed(exe_dst: Path, dest_dir: Path):
    """Best effort detached relaunch of the installed exe. Returns the Popen
    handle or None."""
    try:
        if exe_dst.exists():
            return subprocess.Popen([str(exe_dst)], cwd=str(dest_dir), close_fds=True,
                                    creationflags=_DETACHED_PROCESS)
    except Exception:  # noqa: BLE001
        pass
    return None


def _relaunch_and_verify(exe_dst: Path, dest_dir: Path, grace: float = 25.0) -> bool:
    """Relaunch the freshly-installed exe and confirm it boots. True if it wrote
    _BOOT_OK_MARKER, False meaning roll back, if it could not start, died
    without signalling, or a stale marker refused to clear, which the loop
    would misread as an instant boot. A process still alive at the deadline is
    kept, never killed."""
    marker = Path(dest_dir) / _BOOT_OK_MARKER
    # Clear the stale marker from the prior launch. This must not fail open:
    # a surviving marker passes the loop below instantly, committing a build
    # that never booted and skipping the rollback. Retry the lock hazard like
    # the swap path does, then read a marker that will not die as a bad boot.
    try:
        _retry_locked(lambda: marker.unlink(missing_ok=True), attempts=3, delay=0.2)
    except OSError:
        return False
    proc = _relaunch_installed(exe_dst, dest_dir)
    if proc is None:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        if proc.poll() is not None:     # exited. Give the marker a beat.
            time.sleep(0.3)
            return marker.exists()
        time.sleep(0.25)
    return True                         # alive but silent at the deadline. Keep it.


def _rollback_windows_update(internal_swapped: bool, exe_swapped: bool,
                             internal_dst: Path, internal_bak: Path, internal_new: Path,
                             exe_dst: Path, exe_bak: Path, exe_new: Path) -> None:
    """Restore the previous install after a failed swap or a build that won't
    boot. Restores BOTH exe and _internal, since mixed old/new may not launch.
    Renames only. Whatever is live gets parked on the *.new scratch name. Never
    raises."""
    try:
        if exe_swapped and exe_bak.exists():
            if exe_dst.exists():
                _force_remove(exe_new)
                try:
                    os.replace(exe_dst, exe_new)
                except OSError:
                    pass
            _retry_locked(lambda: os.replace(exe_bak, exe_dst))
    except Exception as exc:  # noqa: BLE001 - never raise, but never stay silent
        # The restore failing leaves the install with no exe at all. Same
        # treatment as the _internal restore below: say so, and leave the
        # user a note naming the backup to rename back by hand.
        _log_update(exe_dst.parent,
                    f"rollback failed to restore {exe_bak.name} ({exc}); "
                    "the install is broken, see RECOVER.txt")
        _drop_recover_note(exe_dst.parent, exe_bak, exe_dst.name)
    try:
        if internal_swapped and internal_bak.exists():
            if internal_dst.exists():
                _force_remove(internal_new)
                try:
                    os.replace(internal_dst, internal_new)
                except OSError:
                    pass
            _retry_locked(lambda: os.replace(internal_bak, internal_dst))
    except Exception as exc:  # noqa: BLE001 - never raise, but never stay silent
        # The restore failing leaves the install with a missing or half _internal.
        # The app can neither launch nor self-repair. Say so, and leave the
        # user a note naming the backup to rename back by hand.
        _log_update(internal_dst.parent,
                    f"rollback failed to restore {internal_bak.name} ({exc}); "
                    "the install is broken, see RECOVER.txt")
        _drop_recover_note(internal_dst.parent, internal_bak, "_internal")
    _force_remove(internal_new)
    _force_remove(exe_new)


def _log_update(dest_dir: Path, msg: str) -> None:
    """Append one timestamped line to the update log in the install dir.
    Best effort. Called from finish_windows_update paths that never raise."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (Path(dest_dir) / _UPDATE_LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _drop_recover_note(dest_dir: Path, backup: Path, target: str) -> None:
    """Drop RECOVER.txt into the install dir after a rollback could not restore
    target, naming the *.nyaa-old backup to rename back by hand.
    Best effort, never raises."""
    kind = "folder" if target == "_internal" else "file"
    try:
        (Path(dest_dir) / "RECOVER.txt").write_text(
            "A NyaaTriggers update failed and the automatic rollback could not\n"
            "restore the previous version. This install is broken: the\n"
            f"{target} {kind} is missing or incomplete, so the app will not start.\n"
            "\n"
            "To recover by hand, in this folder rename\n"
            f"    {backup.name}\n"
            "to\n"
            f"    {target}\n"
            f"and start the app again. If that {kind} is gone, reinstall from\n"
            f"{RELEASES_URL}\n"
            "\n"
            "Once the app starts, delete this RECOVER.txt. While it exists\n"
            "the old backup cleanup stays disabled and backups pile up.\n",
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _mark_rejected(dest_dir: Path, staging_root: Path) -> str:
    """Drop the sentinel naming the build a boot-verify rollback rejected, and
    return its version string, "unknown" when it can't be read. The version
    lives in app_common.py, which a frozen build carries only compiled, so
    the staged source tree is the only place it may be readable. Best effort,
    never raises."""
    version = "unknown"
    try:
        m = re.search(r'^_VERSION\s*=\s*"([^"]+)"',
                      (Path(staging_root) / "app_common.py").read_text(encoding="utf-8"),
                      re.M)
        if m:
            version = m.group(1)
    except Exception:  # noqa: BLE001 - frozen staging keeps no readable source
        pass
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        (Path(dest_dir) / _REJECTED_NAME).write_text(
            f"{stamp}  rejected {version}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return version


def _is_update_staging(dest_dir: Path, staging_root: Path) -> bool:
    """True if staging_root is the _STAGING_PREFIX* dir apply_frozen_windows
    makes next to dest_dir, or the app root extracted inside it, the real
    --staging argument, since the release archive wraps the tree in a
    top-level folder. Both paths must already be resolved."""
    for p in (staging_root, *staging_root.parents):
        if p.parent == dest_dir.parent:
            return p.name.startswith(_STAGING_PREFIX)
    return False


def finish_windows_update(dest_dir: Path, staging_root: Path, old_pid: int,
                          exe_name: str) -> None:
    """Run by the NEW staged exe via --apply-update. Refuses to touch anything
    unless the argv look like a real apply_frozen_windows hand-off, a live old
    pid and staging in a _STAGING_PREFIX* dir next to dest. Waits for the old
    process to exit, copies the new tree into siblings first so it is never
    half-written, then swaps in with renames. The exe is kept present at all
    times, copy-backup plus one atomic overwrite, so a kill mid-swap can't leave
    an exe-less install. Then relaunches the new build and waits for a good-boot
    signal. If it never comes, say antivirus quarantined a fresh DLL, it rolls
    back to the previous version. Rolls back on in-process failure too. Never raises."""
    dest_dir = Path(dest_dir).resolve()
    staging_root = Path(staging_root).resolve()
    # --apply-update feeds these straight from argv, so prove they look like a
    # real apply_frozen_windows hand-off before touching a single file. A live
    # old pid, and staging inside a _STAGING_PREFIX* dir next to the install.
    if old_pid <= 0 or not _is_update_staging(dest_dir, staging_root):
        _log_update(dest_dir, f"refused --apply-update: pid={old_pid}, staging "
                              f"{staging_root} is not a {_STAGING_PREFIX}* "
                              f"sibling of {dest_dir}")
        return
    new_internal = staging_root / "_internal"
    new_exe = staging_root / exe_name
    internal_dst = dest_dir / "_internal"
    exe_dst = dest_dir / exe_name
    # Pid-suffixed names. os.replace always lands on a guaranteed-absent path,
    # and the _BACKUP_SUFFIX ending keeps them inside cleanup_old_backups' glob.
    pid = os.getpid()
    internal_bak = dest_dir / f"_internal.{pid}{_BACKUP_SUFFIX}"
    internal_new = dest_dir / f"_internal.{pid}.new{_BACKUP_SUFFIX}"
    exe_bak = dest_dir / f"{exe_name}.{pid}{_BACKUP_SUFFIX}"
    exe_new = dest_dir / f"{exe_name}.{pid}.new{_BACKUP_SUFFIX}"

    if not _wait_for_pid_exit(old_pid):
        # Old process exit unconfirmed. Swapping would fight its file locks.
        # Leave the install untouched and relaunch it.
        _log_update(dest_dir, f"old process {old_pid} did not exit in time; "
                              "swap skipped, install untouched")
        _relaunch_installed(exe_dst, dest_dir)
        return

    internal_swapped = False
    exe_swapped = False
    try:
        # Step 1, copy the new tree into siblings first. The live install is untouched.
        _force_remove(internal_new)
        # A failed attempt leaves a partial internal_new behind. The retry
        # must copy over it, or it dies on FileExistsError at once.
        _retry_locked(lambda: shutil.copytree(new_internal, internal_new,
                                              dirs_exist_ok=True))
        _force_remove(exe_new)
        _retry_locked(lambda: shutil.copy2(new_exe, exe_new))

        # Step 2, swap _internal by two adjacent renames onto absent targets.
        if internal_dst.exists():
            _retry_locked(lambda: os.replace(internal_dst, internal_bak))
            internal_swapped = True
        _retry_locked(lambda: os.replace(internal_new, internal_dst))

        # Step 3, swap the exe with no absent window. Copy-backup, then one
        # atomic overwrite. A kill here can't leave an exe-less install.
        if exe_dst.exists():
            _force_remove(exe_bak)
            _retry_locked(lambda: shutil.copy2(exe_dst, exe_bak))
            exe_swapped = True
        _retry_locked(lambda: os.replace(exe_new, exe_dst))
    except Exception as exc:  # noqa: BLE001 - keep the install launchable
        _log_update(dest_dir, f"swap failed ({exc}); rolled back")
        _rollback_windows_update(internal_swapped, exe_swapped, internal_dst,
                                 internal_bak, internal_new, exe_dst, exe_bak, exe_new)
        if _install_looks_intact(dest_dir, exe_dst):
            _relaunch_installed(exe_dst, dest_dir)
        return

    # Antivirus can quarantine a freshly written DLL right after the swap,
    # leaving a live-but-broken _internal that can never self-repair. Confirm
    # the new build boots before committing, else roll back. We run from the
    # staged copy, so rollback is always possible.
    try:
        booted = _relaunch_and_verify(exe_dst, dest_dir)
    except Exception:  # noqa: BLE001 - a verifier error reads as a failed boot
        booted = False
    if booted:
        _force_remove(dest_dir / _REJECTED_NAME)   # clear any stale sentinel
        _log_update(dest_dir, "update applied; the new build booted OK")
        return
    try:
        _rollback_windows_update(internal_swapped, exe_swapped, internal_dst,
                                 internal_bak, internal_new, exe_dst, exe_bak, exe_new)
    except Exception:  # noqa: BLE001 - never skip the relaunch below
        pass
    rejected = _mark_rejected(dest_dir, staging_root)
    _log_update(dest_dir, f"the new build failed to boot; rolled back "
                          f"(rejected {rejected})")
    if _install_looks_intact(dest_dir, exe_dst):
        _relaunch_installed(exe_dst, dest_dir)
    # Staging is left for the next launch to sweep. This process runs from
    # inside staging_root, so its own loaded DLLs are still locked.


def _force_remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            # Gone is the goal here. A sharing violation or perms error leaves
            # it for the next-launch sweep. Called from paths that never raise.
            pass


def _looks_like_update_staging(d: Path) -> bool:
    """True if a _STAGING_PREFIX* dir holds an extracted update. The archive's
    app root, top level or one folder down, contains _internal or the staged
    exe. Guards the next-launch sweep against deleting a folder that merely
    matches the name."""
    try:
        root = _archive_app_root(d)
        return (root / "_internal").is_dir() or any(root.glob("*.exe"))
    except OSError:
        return False


def cleanup_old_backups(dest_dir: Path | None = None) -> None:
    """Remove update leftovers. *.nyaa-old backups and stale launcher .part
    temps in the install dir, orphaned .nyaa-update-* staging dirs in its
    parent. Safe every launch."""
    try:
        dest_dir = (dest_dir or install_dir()).resolve()
        live_internal = dest_dir / "_internal"
        try:
            internal_ok = live_internal.is_dir() and any(live_internal.iterdir())
        except OSError:
            internal_ok = False
        # A RECOVER.txt means a rollback could not restore the install and the
        # note names the backup to rename back by hand. Sweeping here would
        # delete that very backup, so leave them all until the note is gone.
        recover_pending = (dest_dir / "RECOVER.txt").exists()
        for entry in dest_dir.glob(f"*{_BACKUP_SUFFIX}"):
            if recover_pending:
                continue
            # Never delete an _internal backup while the live _internal is
            # missing or empty. After a failed update it may be the only intact
            # copy. Restoring it automatically cannot happen here. A missing
            # _internal means the frozen exe cannot load Python, so this code
            # never runs in that state. The shipped NyaaTriggers.sh launcher
            # does that restore before the exe starts.
            if entry.name.startswith("_internal") and not internal_ok:
                continue
            _force_remove(entry)
        # The launcher copy in apply_frozen_linux renames a .part temp into
        # place and a kill mid copy leaks it. The age guard keeps a concurrent
        # update's in flight copy untouched, like the download .part sweep.
        cutoff = time.time() - 3600.0
        for part in dest_dir.glob("NyaaTriggers.sh.*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
        for entry in dest_dir.parent.glob(f"{_STAGING_PREFIX}*"):
            # Only sweep dirs that actually look like updater staging. For a
            # source checkout dest_dir is the source dir, so this glob hits the
            # checkout's PARENT. A user's own folder matching the name must
            # survive, or every launch would delete it.
            if entry.is_dir() and _looks_like_update_staging(entry):
                _force_remove(entry)
    except Exception:  # noqa: BLE001
        pass


# ── Relaunch ───────────────────────────────────────────────────────────────

def relaunch_args() -> tuple[str, list[str]]:
    """The executable and argv to re-exec the app after an update."""
    if is_frozen():
        # sys.executable IS already the app exe.
        return sys.executable, [sys.executable, *sys.argv[1:]]
    return sys.executable, [sys.executable, *sys.argv]


def relaunch() -> None:
    """Replace the current process with a fresh instance. Never returns on
    success."""
    exe, args = relaunch_args()
    # Windowed frozen builds have no console, so sys.stdout and sys.stderr
    # can be None here.
    if sys.stdout is not None:
        sys.stdout.flush()
    if sys.stderr is not None:
        sys.stderr.flush()
    if os.name == "nt":
        # Windows exec* concatenates argv WITHOUT quoting, so any path with a
        # space, Program Files or a First Last user folder, relaunches with
        # mangled argv and fails to start. Spawn a detached child instead.
        subprocess.Popen(args, executable=exe, close_fds=True,
                         creationflags=_DETACHED_PROCESS)
        os._exit(0)
    os.execv(exe, args)
