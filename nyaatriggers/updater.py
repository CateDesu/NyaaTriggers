"""Update source and frozen NyaaTriggers installations without Qt. Git installs pull
changes and refresh dependencies. Frozen installs replace the executable and runtime
while preserving user data. Windows uses a staged process to wait for file locks, apply
the update and check startup before keeping it. Other source copies open the releases
page.
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
import uuid
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from nyaatriggers.paths import source_root
from nyaatriggers.http_fetch import open_response

REPO            = "CateDesu/NyaaTriggers"
API_LATEST_URL  = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL    = f"https://github.com/{REPO}/releases/latest"
LINUX_ASSET     = "NyaaTriggers-linux.tar.gz"
WINDOWS_ASSET   = "NyaaTriggers-windows.zip"
_USER_AGENT     = "NyaaTriggers"
_BACKUP_SUFFIX  = ".nyaa-old"     # marks files left behind for next-launch cleanup
# A normal launch writes this marker after startup. The staged Windows updater checks it
# after swapping files.
_BOOT_OK_MARKER = ".nyaa-boot-ok"
# Keep update outcomes and the rejected build identity in the install directory. Logging
# must not raise.
_UPDATE_LOG_NAME = "nyaatriggers-update.log"
_REJECTED_NAME   = ".nyaa-update-rejected"
_STAGED_VERSION_NAME = ".nyaa-update-version"
# Stage updates beside the install directory for the handoff and later cleanup.
_STAGING_PREFIX  = ".nyaa-update-"
_STAGING_OWNER = ".nyaa-update-owner"
_UPDATE_LOCK = ".nyaa-update.lock"
_LINUX_PENDING = ".nyaa-linux-update"
# Limit release downloads even when Content-Length is missing or incorrect.
_MAX_DOWNLOAD_BYTES = 2 * 1024**3
# Bound release API response size. A separate deadline bounds the read time.
_MAX_RELEASE_BYTES = 4 << 20
# Enforce stall and total deadlines outside the read because socket timeouts reset on
# each received byte.
_READ_STALL_S = 60
_RELEASE_DEADLINE_S = 30
_DOWNLOAD_DEADLINE_S = 3600
# Cache the last release lookup so rate limited checks can still offer a known update.
_RELEASE_CACHE_NAME = "latest_release.json"


def _unblock_reader(resp) -> None:
    """Try to shut down the socket without waiting for the reader to release its buffer
    lock.
    """
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass


class RateLimited(Exception):
    """GitHub reported an exhausted API budget or a secondary rate limit."""


@dataclass
class Release:
    tag: str
    version: str                         # tag with a leading "v" stripped
    html_url: str
    body: str = ""
    assets: dict[str, str] = field(default_factory=dict)   # name -> download url



def _raw_segments(s: str) -> list[str]:
    """Split version segments without removing trailing zeroes."""
    s = s.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    return s.split(".")


def _strip_v(tag: str) -> str:
    """Remove exactly one leading v or V from a tag."""
    return tag[1:] if tag[:1] in ("v", "V") else tag


def parse_version(s: str) -> tuple[int, ...]:
    """Parse leading digits from each version segment and remove trailing zero segments.
    """
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
    """Whether a version segment contains a suffix or other nondigit text."""
    return any(not seg.isdigit() for seg in _raw_segments(s))


def is_newer(remote: str, current: str) -> bool:
    """Compare versions, preferring a final release over a suffixed version with the same
    numbers.
    """
    r, c = parse_version(remote), parse_version(current)
    if r != c:
        return r > c
    return _has_suffix(current) and not _has_suffix(remote)


def is_update_for_here(remote: str, current: str, kind: str | None = None) -> bool:
    """Compare complete version tags. kind remains for compatibility and is ignored."""
    return is_newer(remote, current)



def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_dir() -> Path:
    """Return the source checkout directory when running unfrozen."""
    return source_root()


def install_kind() -> str:
    if is_frozen():
        if sys.platform.startswith("win"):
            return "frozen-windows"
        return "frozen-linux"
    if (source_dir() / ".git").exists():
        return "git"
    return "source"


def can_self_apply(kind: str | None = None) -> bool:
    """Whether this installation can apply updates itself."""
    return (kind or install_kind()) in ("git", "frozen-linux", "frozen-windows")


def _describe_label(base: str, out: str) -> str | None:
    """Read a rolling version from git describe when it shares the current base version.
    """
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-(\d+)-g[0-9a-f]+)?", out.strip())
    if m and (m.group(1) == base or m.group(1).startswith(base + ".")):
        return m.group(1)
    return None


def display_version(base: str, kind: str | None = None) -> str:
    """Return the display version. Frozen builds use their stamp, git checkouts use a
    matching rolling tag, and plain source copies add a source suffix. Failures return
    base without affecting startup.
    """
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
        except Exception:  # noqa: BLE001
            return base
        if r.returncode == 0:
            label = _describe_label(base, r.stdout)
            if label:
                return label
    return base


def install_dir() -> Path:
    """Return the directory to update."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return source_dir()


def is_rejected_update(version: str, dest_dir: Path | None = None) -> bool:
    """Whether this release previously failed to boot and was rolled back."""
    if not version:
        return False
    try:
        with ((dest_dir or install_dir()) / _REJECTED_NAME).open(encoding="utf-8") as marker:
            text = marker.read(512)
    except (OSError, ValueError):
        return False
    match = re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  rejected "
        r"([A-Za-z0-9][A-Za-z0-9._+-]{0,127})\n?", text)
    return bool(match and _strip_v(match.group(1)) == _strip_v(version))


def mark_boot_ok() -> None:
    """Write the frozen build's boot marker after Qt loads so the Windows updater can
    confirm startup.
    """
    if not is_frozen():
        return
    try:
        (install_dir() / _BOOT_OK_MARKER).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass



def _parse_release(data: dict) -> Release:
    tag = data.get("tag_name", "") or ""
    if (not isinstance(tag, str) or len(tag) > 128
            or not re.fullmatch(r"[vV]?[0-9]+(?:\.[0-9]+){0,7}(?:[-+][A-Za-z0-9.-]+)?", tag)):
        raise ValueError("Invalid release tag")
    raw_assets = data.get("assets", [])
    if not isinstance(raw_assets, list):
        raise ValueError("Invalid release assets")
    assets = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in raw_assets
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]
        and isinstance(a.get("browser_download_url"), str) and a["browser_download_url"]
    }
    return Release(
        tag=tag,
        version=_strip_v(tag),
        html_url=(data.get("html_url") or RELEASES_URL)
                 if isinstance(data.get("html_url"), str) else RELEASES_URL,
        body=data.get("body") if isinstance(data.get("body"), str) else "",
        assets=assets,
    )


def fetch_latest_release(timeout: int = 8, channel: str = "stable") -> Release:
    """Fetch and cache the latest stable release. Raise on network or parse failure,
    including RateLimited for API limits. channel remains for compatibility and is
    ignored.
    """
    req = urllib.request.Request(API_LATEST_URL, headers={"User-Agent": _USER_AGENT})
    deadline = time.monotonic() + _RELEASE_DEADLINE_S
    try:
        with open_response(req, timeout, min(deadline, time.monotonic() + _READ_STALL_S)) as resp:
            # Read in a helper so the caller can enforce the deadline.
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
            last_seen = progress[0]
            while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                if progress[0] == last_seen or time.monotonic() > deadline:
                    # Shut down the socket to wake the reader without waiting for its
                    # read lock.
                    _unblock_reader(resp)
                    raise OSError("Release info timed out after 30 seconds.")
                last_seen = progress[0]
            if reader_error[0]:
                raise reader_error[0]
            body = b"".join(chunks)
        if len(body) > _MAX_RELEASE_BYTES:
            # Report an oversized body directly instead of passing truncated JSON to the
            # parser.
            raise OSError(
                f"Release info exceeded the {_MAX_RELEASE_BYTES >> 20} MB safety cap")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        # Handle both exhausted API budgets and secondary Retry-After limits through the
        # cache. Close HTTPError because it is also a response object.
        exc.close()
        if exc.code == 429 or (exc.code == 403 and (exc.headers.get("X-RateLimit-Remaining") == "0"
                                                   or "Retry-After" in exc.headers)):
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
    """Try to save the release lookup for rate limit fallback."""
    path = _release_cache_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({
            "tag":      release.tag,
            "version":  release.version,
            "html_url": release.html_url,
            "body":     release.body,
            "assets":   release.assets,
        }), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_cached_release() -> Release | None:
    """Return the last cached release or None."""
    try:
        with _release_cache_path().open(encoding="utf-8") as cached:
            raw = cached.read(_MAX_RELEASE_BYTES + 1)
        if len(raw) > _MAX_RELEASE_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tag"), str) or not data["tag"]:
        return None
    assets = data.get("assets")
    if not isinstance(assets, dict):
        assets = {}
    try:
        return _parse_release({
            "tag_name": data["tag"], "html_url": data.get("html_url"),
            "body": data.get("body"),
            "assets": [{"name": k, "browser_download_url": v} for k, v in assets.items()],
        })
    except ValueError:
        return None


def asset_for_platform(release: Release) -> str | None:
    """Download URL of the archive for this platform, or None."""
    kind = install_kind()
    if kind == "frozen-linux":
        return release.assets.get(LINUX_ASSET)
    if kind == "frozen-windows":
        return release.assets.get(WINDOWS_ASSET)
    return None



def download(url: str, dest: Path, progress_cb: Callable[[int, int], None] | None = None,
             timeout: int = 60, max_bytes: int | None = None) -> None:
    """Download to a temporary file and rename on success. progress_cb receives downloaded
    and total bytes, with total zero when unknown. Remove the partial file on failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # Use a unique temporary file so concurrent downloads cannot truncate each other.
    part = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    limit = _MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes
    deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
    try:
        with open_response(req, timeout, min(deadline, time.monotonic() + _READ_STALL_S)) as resp:
            # Treat invalid lengths as unknown. The byte limit still applies.
            try:
                total = int(resp.headers.get("Content-Length", 0) or 0)
            except ValueError:
                total = 0
            total = max(0, total)
            if total > limit:
                raise OSError(f"Download exceeds the {limit} byte safety cap")
            if total:
                free = shutil.disk_usage(dest.parent).free
                if free < total + (32 << 20):   # keep ~32 MB headroom
                    raise OSError(
                        f"Not enough free space to download the update: need "
                        f"~{total >> 20} MB, have {free >> 20} MB free.")
            # Read in a helper so the caller can enforce stall and total deadlines.
            done = threading.Event()
            progress = [0]
            reader_error = [None]

            def _reader() -> None:
                try:
                    with part.open("wb") as f:
                        read_chunk = getattr(resp, "read1", resp.read)
                        notified = 0
                        last_notice = time.monotonic()
                        while True:
                            chunk = read_chunk(262144)
                            if not chunk:
                                break
                            f.write(chunk)
                            progress[0] += len(chunk)
                            if progress[0] > limit:
                                raise OSError(
                                    f"Download exceeded the {limit} byte "
                                    "safety cap (missing or lying Content-Length)")
                            now = time.monotonic()
                            if progress_cb and (progress[0] - notified >= 262144 or now - last_notice >= .2):
                                progress_cb(progress[0], total)
                                notified = progress[0]
                                last_notice = now
                        if progress_cb and progress[0] != notified:
                            progress_cb(progress[0], total)
                except BaseException as exc:
                    reader_error[0] = exc
                finally:
                    done.set()

            threading.Thread(target=_reader, daemon=True).start()
            last_seen = progress[0]
            last_change = time.monotonic()
            while not done.wait(timeout=min(_READ_STALL_S, max(0.0, deadline - time.monotonic()))):
                now = time.monotonic()
                if progress[0] == last_seen or now > deadline:
                    # Shut down the socket to wake the reader without waiting for its
                    # read lock.
                    _unblock_reader(resp)
                    # Report a stall only after the full quiet window has elapsed.
                    if now - last_change >= _READ_STALL_S:
                        raise OSError(
                            f"Download stalled, no new bytes for {_READ_STALL_S} seconds.")
                    raise OSError("Download timed out after 60 minutes.")
                last_seen = progress[0]
                last_change = now
            if reader_error[0]:
                raise reader_error[0]
        # A connection can close early without raising.
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
    """Check an archive against its published checksum sidecar. Missing, unreadable or
    mismatched checksums fail verification.
    """
    url = release.assets.get(asset_name + ".sha256")
    if not url:
        return False, "no checksum published for this release"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        deadline = time.monotonic() + timeout
        with open_response(req, timeout, deadline) as resp:
            done = threading.Event()
            body, errors = [], []

            def read():
                try:
                    body.append(resp.read(4097))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    done.set()

            threading.Thread(target=read, daemon=True).start()
            if not done.wait(max(0, deadline - time.monotonic())):
                _unblock_reader(resp)
                raise TimeoutError("checksum download timed out")
            if errors:
                raise errors[0]
            if len(body[0]) > 4096:
                raise ValueError("checksum response is too large")
            text = body[0].decode("utf-8", "replace")
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



def _git_env() -> dict[str, str]:
    # Do not open a password dialog from a background update check.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
            "SSH_ASKPASS": "", "GCM_INTERACTIVE": "Never"}


def git_covers_upstream(repo_dir: Path | None = None, timeout: int = 10) -> bool:
    """Whether HEAD contains the upstream tip. Return False when this cannot be confirmed
    so callers can use version comparison.
    """
    repo = str(repo_dir or source_dir())

    def _git(*args: str) -> "subprocess.CompletedProcess | None":
        try:
            r = subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                env=_git_env(),
            )
        except Exception:  # noqa: BLE001
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
    # An upstream tip already contained in HEAD has nothing new. An unfetched tip
    # returns False.
    return _git("merge-base", "--is-ancestor", sha, "HEAD") is not None


def _install_requirements(repo_dir: Path) -> tuple[bool, str] | None:
    """Install requirements with the running interpreter after a successful pull. Repeating
    this step repairs a previously failed dependency update. Return None if no
    requirements file exists, otherwise success and detail.
    """
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


def _git_pull(repo_dir: Path) -> subprocess.CompletedProcess:
    """Pull with tags and fast forward only, using English output for conflict parsing.
    """
    return subprocess.run(
        ["git", "-C", str(repo_dir), "pull", "--ff-only", "--tags"],
        capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
        env={**_git_env(), "LC_ALL": "C"},
    )


def _stale_cactbot_conflicts(detail: str) -> list[str]:
    """Identify untracked cactbot downloads blocking a pull. Return no paths if any
    conflict is unrelated.
    """
    lines = detail.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if "untracked working tree files would be overwritten by merge"
                  in ln), None)
    if start is None:
        return []
    paths = []
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if re.fullmatch(r"timelines/[a-z0-9][a-z0-9_-]*\.cactbot\.txt", stripped):
            paths.append(stripped)
        elif not stripped or stripped.startswith(("Please ", "hint:", "Aborting")):
            break
        else:
            return []
    return paths


def _preserve_untracked(repo_dir: Path, rel_paths: list[str]) -> Path:
    """Move conflicting downloads aside without discarding local edits."""
    backup_root = repo_dir / ".nyaa-timeline-backups"
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="update-", dir=backup_root))
    for rel in rel_paths:
        saved = backup / rel
        saved.parent.mkdir(parents=True, exist_ok=True)
        os.replace(repo_dir / rel, saved)
    return backup


def apply_git(repo_dir: Path | None = None) -> tuple[bool, str]:
    """Pull changes and tags, then refresh dependencies. Move conflicting old timeline
    downloads aside and retry once. Return success and a message.
    """
    repo_dir = repo_dir or source_dir()
    try:
        r = _git_pull(repo_dir)
    except FileNotFoundError:
        return False, "git is not installed or not on PATH."
    except Exception as exc:  # noqa: BLE001
        return False, f"git pull failed: {exc}"
    cleared = 0
    backup = None
    if r.returncode != 0:
        stale = _stale_cactbot_conflicts(r.stderr.strip() or r.stdout.strip())
        if stale:
            try:
                backup = _preserve_untracked(repo_dir, stale)
                cleared = len(stale)
                r = _git_pull(repo_dir)
            except Exception as exc:
                return False, (f"Could not retry git pull: {exc}\n"
                               f"Saved timelines are in {repo_dir / '.nyaa-timeline-backups'}")
    if r.returncode == 0:
        msg = r.stdout.strip() or "Updated."
        if cleared:
            plural = "s" if cleared != 1 else ""
            msg = (f"Saved {cleared} stale cactbot timeline download{plural} "
                   f"that blocked the pull to {backup}.\n\n" + msg)
        deps = _install_requirements(repo_dir)
        if deps is not None:
            deps_ok, detail = deps
            if not deps_ok:
                return False, (
                    msg + "\n\nThe code was updated, but installing the Python "
                    f"dependencies failed:\n{detail}\n"
                    "Try Install again, or run `pip install -r requirements.txt` "
                    "yourself before restarting the program.")
            msg += "\n\nPython dependencies are up to date."
        return True, msg
    detail = (r.stderr.strip() or r.stdout.strip() or "unknown error")
    if backup is not None:
        detail += f"\nSaved timelines are in {backup}"
    return False, (
        f"git pull failed:\n{detail}\n\n"
        "If you have local edits to tracked files, stash or revert them and try "
        "again, or update manually."
    )



def _archive_app_root(extracted_to: Path) -> Path:
    """Find the executable and runtime directory within an extracted archive."""
    if (extracted_to / "_internal").is_dir():
        return extracted_to
    nested = extracted_to / "NyaaTriggers"
    if (nested / "_internal").is_dir():
        return nested
    for child in sorted(extracted_to.iterdir()):
        if child.is_dir() and (child / "_internal").is_dir():
            return child
    return nested


@contextmanager
def _update_lock(dest_dir: Path):
    """Keep swaps and cleanup from changing the same install at once."""
    with (dest_dir / _UPDATE_LOCK).open("a+b") as lock:
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract a tar archive while rejecting paths and links outside the destination.
    Preserve relative links within the bundled runtime.
    """
    dest = dest.resolve()
    dest_s = str(dest)

    def _inside(p: Path) -> bool:
        ps = str(p)
        return ps == dest_s or ps.startswith(dest_s + os.sep)

    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            # Before extraction, resolve checks only the path layout.
            target = (dest / member.name).resolve()
            if not _inside(target):
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym():
                # Symlink targets are relative to the link directory.
                link_target = (target.parent / member.linkname).resolve()
                if not _inside(link_target):
                    raise RuntimeError(
                        f"unsafe symlink target in archive: {member.name} -> {member.linkname}")
            elif member.islnk():
                # Hardlink targets are relative to the archive root.
                link_target = (dest / member.linkname).resolve()
                if not _inside(link_target):
                    raise RuntimeError(
                        f"unsafe hardlink target in archive: {member.name} -> {member.linkname}")
        # The data filter checks paths again during extraction to catch escapes through
        # earlier symlink members. Relative links within the build remain valid.
        tf.extractall(dest, filter="data")


def apply_frozen_linux(tar_path: Path, dest_dir: Path | None = None,
                       exe_name: str | None = None) -> tuple[bool, str]:
    """Apply one Linux update while holding the install lock."""
    dest_dir = (dest_dir or install_dir()).resolve()
    try:
        with _update_lock(dest_dir):
            if (dest_dir / _LINUX_PENDING).exists():
                return False, "An interrupted update needs recovery. Start NyaaTriggers.sh first."
            return _apply_frozen_linux(tar_path, dest_dir, exe_name)
    except OSError as exc:
        return False, f"Could not lock the install folder. Another update may be running: {exc}"


def _apply_frozen_linux(tar_path: Path, dest_dir: Path,
                        exe_name: str | None) -> tuple[bool, str]:
    """Replace the Linux executable and runtime while preserving user data. Keep backups
    for cleanup on the next launch and return success and a message.
    """
    dest_dir = (dest_dir or install_dir()).resolve()
    exe_name = exe_name or Path(sys.executable).name
    if Path(exe_name).name != exe_name or exe_name in ("", ".", "..", "_internal") or "\n" in exe_name:
        return False, "Invalid executable filename"
    if not os.access(dest_dir, os.W_OK):
        return False, f"No write permission for the install folder:\n{dest_dir}"

    staging = None
    try:
        # Staging can fail if the install parent is not writable. Return the failure
        # through the usual result.
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(dest_dir.parent)))
        (staging / _STAGING_OWNER).write_text(str(dest_dir), encoding="utf-8")
        _safe_extract_tar(tar_path, staging)
        new_root = _archive_app_root(staging)
        new_exe = new_root / "NyaaTriggers"
        new_internal = new_root / "_internal"
        if not new_internal.is_dir() or not any(new_internal.iterdir()) or not new_exe.is_file():
            return False, "Downloaded update is missing expected files (exe / _internal)."

        new_launcher = new_root / "NyaaTriggers.sh"
        if not new_launcher.is_file():
            return False, "Downloaded update is missing the recovery launcher."
        with new_launcher.open("rb") as launcher:
            if b"# nyaa-linux-recovery: 1" not in launcher.read(256).splitlines():
                return False, "Downloaded update has an incompatible recovery launcher."

        os.chmod(new_exe, 0o755)
        launcher_dst = dest_dir / "NyaaTriggers.sh"
        tmp = dest_dir / f"NyaaTriggers.sh.{os.getpid()}.{threading.get_ident()}.part"
        try:
            shutil.copy2(new_launcher, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, launcher_dst)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        internal_dst = dest_dir / "_internal"
        exe_dst = dest_dir / exe_name
        generation = uuid.uuid4().int
        internal_backup = dest_dir / f"_internal.{generation}{_BACKUP_SUFFIX}"
        exe_backup = dest_dir / f"{exe_name}.{generation}{_BACKUP_SUFFIX}"
        internal_swapped = False
        exe_swapped = False
        pending = dest_dir / _LINUX_PENDING
        pending_tmp = pending.with_name(f"{pending.name}.{generation}.tmp")
        try:
            if exe_dst.exists():
                shutil.copy2(exe_dst, exe_backup)
            with pending_tmp.open("w", encoding="utf-8") as record:
                record.write(f"{generation}\n{exe_name}\n")
                record.flush()
                os.fsync(record.fileno())
            os.replace(pending_tmp, pending)
            # Swap the runtime with adjacent renames. Open files remain usable by the
            # running process.
            if internal_dst.exists():
                os.replace(internal_dst, internal_backup)
                internal_swapped = True
            # Require a same filesystem rename. A mount boundary must fail rather than
            # fall back to a partial copy.
            os.replace(str(new_internal), str(internal_dst))

            # Back up the executable before replacing it atomically so it is never
            # absent.
            os.replace(str(new_exe), str(exe_dst))
            exe_swapped = True
            pending.unlink()
        except Exception:
            if internal_swapped and internal_backup.exists():
                restore_backup, restore_target = internal_backup, "_internal"
                try:
                    _force_remove(internal_dst)
                    os.replace(internal_backup, internal_dst)
                    if exe_swapped and exe_backup.exists():
                        restore_backup, restore_target = exe_backup, exe_name
                        os.replace(exe_backup, exe_dst)
                    pending.unlink(missing_ok=True)
                except Exception as exc:
                    _log_update(dest_dir, f"Linux rollback failed: {exc}")
                    if restore_backup.exists():
                        _drop_recover_note(dest_dir, restore_backup, restore_target)
            elif not internal_swapped:
                pending.unlink(missing_ok=True)
            raise
        finally:
            try:
                pending_tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return True, "Update installed."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not install the update: {exc}"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


# Windows updates run from a staged copy after the installed process exits and releases
# its executable and DLLs.

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a zip archive while rejecting paths outside the destination."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise RuntimeError(f"unsafe path in archive: {name}")
        zf.extractall(dest)


def _dir_writable(d: Path) -> bool:
    """Test directory access by creating a file because Windows os.access does not check
    ACLs.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(d), prefix=".nyaa-wtest-"):
            pass
        return True
    except OSError:
        return False


def apply_frozen_windows(zip_path: Path, dest_dir: Path | None = None,
                         exe_name: str | None = None, version: str = "") -> tuple[bool, str]:
    """Stage the Windows update and launch its updater. A successful handoff returns
    __windows_handoff__ and requires the caller to quit promptly. Failures leave the
    installed build unchanged.
    """
    dest_dir = (dest_dir or install_dir()).resolve()
    exe_name = exe_name or Path(sys.executable).name
    if not _valid_windows_exe_name(exe_name):
        return False, "Invalid executable filename"
    if not _dir_writable(dest_dir):
        return False, f"No write permission for the install folder:\n{dest_dir}"

    staging = None
    try:
        # Allow space for extraction, the replacement copy and backups on the install
        # volume.
        with zipfile.ZipFile(zip_path) as zf:
            unpacked = sum(i.file_size for i in zf.infolist())
        need = unpacked * 3 + (64 << 20)
        # Check both volumes because the install directory may be a separate mount.
        for volume in (dest_dir, dest_dir.parent):
            free = shutil.disk_usage(volume).free
            if free < need:
                return False, (f"Not enough free space on the install drive to update: "
                               f"need ~{need >> 20} MB, have {free >> 20} MB free.")

        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(dest_dir.parent)))
        (staging / _STAGING_OWNER).write_text(str(dest_dir), encoding="utf-8")
        _safe_extract_zip(zip_path, staging)
        new_root = _archive_app_root(staging)
        new_exe = new_root / "NyaaTriggers.exe"
        if not (new_root / "_internal").is_dir() or not new_exe.exists():
            shutil.rmtree(staging, ignore_errors=True)
            return False, "Downloaded update is missing expected files (exe / _internal)."
        staged_version = version if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version) else "unknown"
        (new_root / _STAGED_VERSION_NAME).write_text(staged_version, encoding="utf-8")
        if exe_name != new_exe.name:
            renamed = new_root / exe_name
            os.replace(new_exe, renamed)
            new_exe = renamed
        # Detach the staged updater so it outlives this process. Windows rejects
        # combining DETACHED_PROCESS with CREATE_NO_WINDOW.
        cmd = [str(new_exe), "--apply-update",
               "--dest", str(dest_dir),
               "--staging", str(new_root),
               "--pid", str(os.getpid()),
               "--exe-name", exe_name]
        proc = subprocess.Popen(
            cmd, cwd=str(new_root), close_fds=True,
            creationflags=_DETACHED_PROCESS,
        )
        # Confirm the staged updater survives launch before closing this process.
        for _ in range(20):
            time.sleep(0.2)
            rc = proc.poll()
            if rc is not None:
                shutil.rmtree(staging, ignore_errors=True)
                if rc == 0:
                    # The staged updater exited normally and logged why it refused the
                    # handoff.
                    return False, (f"Update refused. See {_UPDATE_LOG_NAME} in the "
                                   "program folder. Download the update manually.")
                return False, ("The staged updater was blocked from starting "
                               "(antivirus may have quarantined it). Please use the "
                               "manual download.")
        return True, "__windows_handoff__"
    except Exception as exc:  # noqa: BLE001
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        return False, f"Could not stage the update: {exc}"


def _wait_for_pid_exit(pid: int, timeout: float = 90.0) -> bool:
    """Wait for a process to exit and release file locks. Prefer a Windows process handle,
    falling back to tasklist. Return False on timeout and True for nonpositive PIDs.
    """
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    # The wait handle tracks the original process even if its PID is reused.
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
        # An OpenProcess failure does not prove exit. Confirm through tasklist.
    except Exception:  # noqa: BLE001
        pass
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:  # noqa: BLE001
            time.sleep(3.0)
            continue
        out = (r.stdout or "").lstrip()
        # Accept exit only after a successful probe with no process row. Localized INFO
        # messages can have a space before the colon. Errors or unexpected output leave
        # the exit unconfirmed.
        if r.returncode == 0 and (not out or out.startswith(("INFO:", "INFO :"))):
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return False


def _retry_locked(fn, attempts: int = 30, delay: float = 0.5):
    """Retry temporary permission errors from file locks, raising the last error if retries
    fail.
    """
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def _install_looks_intact(dest_dir: Path, exe_dst: Path) -> bool:
    """Check that the executable exists and the runtime directory is nonempty."""
    try:
        internal = dest_dir / "_internal"
        if not exe_dst.exists() or not internal.is_dir():
            return False
        return any(internal.iterdir())
    except OSError:
        return False


def _relaunch_installed(exe_dst: Path, dest_dir: Path):
    """Try to relaunch the installed executable detached and return its process handle.
    """
    try:
        if exe_dst.exists():
            return subprocess.Popen([str(exe_dst)], cwd=str(dest_dir), close_fds=True,
                                    creationflags=_DETACHED_PROCESS)
    except Exception:  # noqa: BLE001
        pass
    return None


def _relaunch_and_verify(exe_dst: Path, dest_dir: Path, grace: float = 25.0) -> bool:
    """Relaunch the installed build and accept a boot marker or a process still alive at
    the deadline. Reject failed starts and stale markers that cannot be cleared. Never
    kill a process still starting.
    """
    marker = Path(dest_dir) / _BOOT_OK_MARKER
    # Clear the old boot marker first. A marker that cannot be removed would falsely
    # confirm startup.
    try:
        _retry_locked(lambda: marker.unlink(missing_ok=True), attempts=3, delay=0.2)
    except OSError:
        return False
    proc = _relaunch_installed(exe_dst, dest_dir)
    if proc is None:
        return False

    def confirmed():
        try:
            with marker.open("rb") as boot:
                token = boot.read(65)
            return len(token) <= 64 and token.strip() == str(proc.pid).encode("ascii")
        except OSError:
            return False

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if confirmed():
            return True
        if proc.poll() is not None:     # Allow time for the marker after process exit.
            time.sleep(0.3)
            return confirmed()
        time.sleep(0.25)
    _log_update(dest_dir, "new build is still running without a boot marker")
    return True


def _rollback_windows_update(internal_swapped: bool, exe_swapped: bool,
                             internal_dst: Path, internal_bak: Path, internal_new: Path,
                             exe_dst: Path, exe_bak: Path, exe_new: Path) -> None:
    """Restore both executable and runtime after a failed update. Move current files aside
    and restore backups without raising.
    """
    try:
        if exe_swapped and exe_bak.exists():
            if exe_dst.exists():
                _force_remove(exe_new)
                try:
                    os.replace(exe_dst, exe_new)
                except OSError:
                    pass
            _retry_locked(lambda: os.replace(exe_bak, exe_dst))
    except Exception as exc:  # noqa: BLE001
        # If restore fails, leave a recovery note naming the executable backup.
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
    except Exception as exc:  # noqa: BLE001
        # If runtime restore fails, leave a recovery note naming its backup.
        _log_update(internal_dst.parent,
                    f"rollback failed to restore {internal_bak.name} ({exc}); "
                    "the install is broken, see RECOVER.txt")
        _drop_recover_note(internal_dst.parent, internal_bak, "_internal")
    _force_remove(internal_new)
    _force_remove(exe_new)


def _log_update(dest_dir: Path, msg: str) -> None:
    """Try to append a timestamped update log entry."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (Path(dest_dir) / _UPDATE_LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _drop_recover_note(dest_dir: Path, backup: Path, target: str) -> None:
    """Try to write a recovery note naming the backup that must be restored manually."""
    kind = "folder" if target == "_internal" else "file"
    try:
        (Path(dest_dir) / "RECOVER.txt").write_text(
            "The update and rollback failed. NyaaTriggers cannot start because\n"
            f"the {target} {kind} is missing or incomplete.\n"
            "\n"
            "In this folder, rename\n"
            f"    {backup.name}\n"
            "to\n"
            f"    {target}\n"
            f"and restart NyaaTriggers. If the backup {kind} is gone, reinstall from\n"
            f"{RELEASES_URL}\n"
            "\n"
            "After a successful start, delete this RECOVER.txt to allow old\n"
            "backups to be cleaned up.\n",
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _mark_rejected(dest_dir: Path, staging_root: Path) -> str:
    """Try to record the rejected release identity from staging."""
    version = "unknown"
    for candidate_path in (Path(staging_root) / "_internal" / "nyaatriggers.version",
                           Path(staging_root) / _STAGED_VERSION_NAME):
        try:
            with candidate_path.open(encoding="utf-8") as staged:
                candidate = staged.read(129).strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", candidate):
                version = candidate
                break
        except Exception:
            pass
    if version == "unknown":
        for relative in ("nyaatriggers/app_common.py", "app_common.py"):
            try:
                m = re.search(r'^_VERSION\s*=\s*"([^"]+)"',
                              (Path(staging_root) / relative).read_text(encoding="utf-8"), re.M)
                if m:
                    version = m.group(1)
                    break
            except Exception:
                pass
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        (Path(dest_dir) / _REJECTED_NAME).write_text(
            f"{stamp}  rejected {version}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return version


def _is_update_staging(dest_dir: Path, staging_root: Path) -> bool:
    """Check that the resolved staging path is an updater directory beside the install or
    its extracted program root.
    """
    for p in (staging_root, *staging_root.parents):
        if p.parent == dest_dir.parent:
            return p.name.startswith(_STAGING_PREFIX)
    return False


def _valid_windows_exe_name(name: str) -> bool:
    return (bool(name) and name not in (".", "..", "_internal")
            and "\x00" not in name and PureWindowsPath(name).name == name
            and not PureWindowsPath(name).drive)


def finish_windows_update(dest_dir: Path, staging_root: Path, old_pid: int,
                          exe_name: str) -> None:
    """Run the staged Windows swap with exclusive access to the install."""
    try:
        with _update_lock(Path(dest_dir).resolve()):
            _finish_windows_update(dest_dir, staging_root, old_pid, exe_name)
    except OSError as exc:
        _log_update(dest_dir, f"could not lock the install folder; swap skipped: {exc}")


def _finish_windows_update(dest_dir: Path, staging_root: Path, old_pid: int,
                           exe_name: str) -> None:
    """Finish the staged Windows update after validating the handoff and waiting for the
    old process. Prepare replacement files, swap with backups, and relaunch to check
    startup. Restore backups on failure and never raise.
    """
    dest_dir = Path(dest_dir).resolve()
    staging_root = Path(staging_root).resolve()
    # Validate the PID and staging location before accepting the command line handoff.
    if (old_pid <= 0 or not _valid_windows_exe_name(exe_name)
            or not _is_update_staging(dest_dir, staging_root)):
        _log_update(dest_dir, f"refused --apply-update: pid={old_pid}, staging "
                              f"{staging_root} is not a {_STAGING_PREFIX}* "
                              f"sibling of {dest_dir}")
        return
    new_internal = staging_root / "_internal"
    new_exe = staging_root / exe_name
    internal_dst = dest_dir / "_internal"
    exe_dst = dest_dir / exe_name
    # Use unique absent backup paths with the suffix recognized by cleanup.
    pid = os.getpid()
    internal_bak = dest_dir / f"_internal.{pid}{_BACKUP_SUFFIX}"
    internal_new = dest_dir / f"_internal.{pid}.new{_BACKUP_SUFFIX}"
    exe_bak = dest_dir / f"{exe_name}.{pid}{_BACKUP_SUFFIX}"
    exe_new = dest_dir / f"{exe_name}.{pid}.new{_BACKUP_SUFFIX}"

    if not _wait_for_pid_exit(old_pid):
        # Leave the install untouched while the old process may still hold its files.
        _log_update(dest_dir, f"old process {old_pid} did not exit in time; "
                              "swap skipped, install untouched")
        _relaunch_installed(exe_dst, dest_dir)
        return

    internal_swapped = False
    exe_swapped = False
    try:
        # Prepare replacement files beside the live install before swapping.
        _force_remove(internal_new)
        # A retry must overwrite files left by a failed copy.
        _retry_locked(lambda: shutil.copytree(new_internal, internal_new,
                                              dirs_exist_ok=True))
        _force_remove(exe_new)
        _retry_locked(lambda: shutil.copy2(new_exe, exe_new))

        if internal_dst.exists():
            _retry_locked(lambda: os.replace(internal_dst, internal_bak))
            internal_swapped = True
        _retry_locked(lambda: os.replace(internal_new, internal_dst))

        # Back up and atomically replace the executable so it is never absent.
        if exe_dst.exists():
            _force_remove(exe_bak)
            _retry_locked(lambda: shutil.copy2(exe_dst, exe_bak))
            exe_swapped = True
        _retry_locked(lambda: os.replace(exe_new, exe_dst))
    except Exception as exc:  # noqa: BLE001
        _log_update(dest_dir, f"swap failed ({exc}); rolled back")
        _rollback_windows_update(internal_swapped, exe_swapped, internal_dst,
                                 internal_bak, internal_new, exe_dst, exe_bak, exe_new)
        if _install_looks_intact(dest_dir, exe_dst):
            _relaunch_installed(exe_dst, dest_dir)
        return

    # Check that the new build starts before keeping it. The staged process can restore
    # the backups if startup fails.
    try:
        booted = _relaunch_and_verify(exe_dst, dest_dir)
    except Exception:  # noqa: BLE001
        booted = False
    if booted:
        _force_remove(dest_dir / _REJECTED_NAME)
        _log_update(dest_dir, "update applied, keeping the new build")
        return
    try:
        _rollback_windows_update(internal_swapped, exe_swapped, internal_dst,
                                 internal_bak, internal_new, exe_dst, exe_bak, exe_new)
    except Exception:  # noqa: BLE001
        pass
    rejected = _mark_rejected(dest_dir, staging_root)
    _log_update(dest_dir, f"the new build failed to boot; rolled back "
                          f"(rejected {rejected})")
    if _install_looks_intact(dest_dir, exe_dst):
        _relaunch_installed(exe_dst, dest_dir)
    # Leave staging for the next launch because this process still holds its DLLs open.


def _force_remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            # Leave locked files for the next launch. Cleanup must not raise.
            pass


def _looks_like_update_staging(d: Path) -> bool:
    """Check for an extracted update before removing a directory with a staging name."""
    try:
        root = _archive_app_root(d)
        return (root / "_internal").is_dir() or any(root.glob("*.exe"))
    except OSError:
        return False


def cleanup_old_backups(dest_dir: Path | None = None) -> None:
    """Sweep completed updates while no swap is using their recovery files."""
    try:
        dest_dir = (dest_dir or install_dir()).resolve()
        with _update_lock(dest_dir):
            _cleanup_old_backups(dest_dir)
    except OSError:
        pass


def _cleanup_old_backups(dest_dir: Path) -> None:
    """Remove completed update backups, stale launcher temporary files and orphaned staging
    directories.
    """
    try:
        dest_dir = (dest_dir or install_dir()).resolve()
        live_internal = dest_dir / "_internal"
        try:
            internal_ok = live_internal.is_dir() and any(live_internal.iterdir())
        except OSError:
            internal_ok = False
        # Preserve backups while RECOVER.txt still names files needed for manual
        # recovery.
        recover_pending = ((dest_dir / "RECOVER.txt").exists()
                           or (dest_dir / _LINUX_PENDING).exists() or not internal_ok)
        for entry in dest_dir.glob(f"*{_BACKUP_SUFFIX}"):
            if recover_pending:
                continue
            # Keep runtime backups when the live runtime is missing or empty. The shell
            # launcher restores them before Python can start.
            if entry.name.startswith("_internal") and not internal_ok:
                continue
            _force_remove(entry)
        # Remove stale launcher temporary files while preserving recent copies from
        # another update.
        cutoff = time.time() - 3600.0
        for part in dest_dir.glob("NyaaTriggers.sh.*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
        for entry in dest_dir.parent.glob(f"{_STAGING_PREFIX}*"):
            # Check the staging contents before removing a matching directory beside the
            # install.
            if entry.is_dir() and not entry.is_symlink() and _looks_like_update_staging(entry):
                try:
                    owner = entry / _STAGING_OWNER
                    if (owner.read_text(encoding="utf-8") == str(dest_dir)
                            and owner.stat().st_mtime < time.time() - 86400):
                        _force_remove(entry)
                except (OSError, ValueError):
                    pass
    except Exception:  # noqa: BLE001
        pass



def relaunch_args() -> tuple[str, list[str]]:
    """Return the executable and arguments for relaunching after an update."""
    if is_frozen():
        return sys.executable, [sys.executable, *sys.argv[1:]]
    return sys.executable, [sys.executable, *sys.argv]


def relaunch() -> None:
    """Replace the current process with a fresh instance. Never returns on
    success."""
    exe, args = relaunch_args()
    # Windowed builds may have no stdout or stderr.
    if sys.stdout is not None:
        sys.stdout.flush()
    if sys.stderr is not None:
        sys.stderr.flush()
    if os.name == "nt":
        # Spawn a detached child on Windows because exec does not quote arguments
        # containing spaces.
        subprocess.Popen(args, executable=exe, close_fds=True,
                         creationflags=_DETACHED_PROCESS)
        os._exit(0)
    os.execv(exe, args)
