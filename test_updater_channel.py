"""Channel-collapse contract test: one stable channel only.

The Stable/Master split is gone. fetch_latest_release keeps its channel
argument for call-site compatibility but IGNORES it: every call performs the
same /releases/latest lookup and returns the parsed release, whatever channel
is passed. The _pick_master_release helper (which picked the highest-versioned
pre-release off the /releases list) is deleted. Pinned here with a stubbed
urlopen, plus the surviving version-comparison rules for git/source vs frozen
installs.

Run directly:  python3 test_updater_channel.py   (exit 0 = all pass)
"""
import email.message
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


PAYLOAD = json.dumps({
    "tag_name": "v1.2.3",
    "html_url": "https://example.test/releases/v1.2.3",
    "body": "release notes",
    "assets": [{"name": "NyaaTriggers-linux.tar.gz",
                "browser_download_url": "https://example.test/dl"}],
}).encode()
ERROR_PAYLOAD = json.dumps({"message": "API rate limit exceeded"}).encode()


def fetch_stubbed(channel=None, payload=PAYLOAD):
    """Run fetch_latest_release with urllib.request.urlopen stubbed out.
    channel=None calls it with no channel argument. Returns
    (release_or_None, urls_hit, error_or_None)."""
    urls = []

    class Resp:
        def read(self, *args):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        urls.append(getattr(req, "full_url", req))
        return Resp()

    orig = updater.urllib.request.urlopen
    updater.urllib.request.urlopen = fake_urlopen
    try:
        if channel is None:
            return updater.fetch_latest_release(), urls, None
        return updater.fetch_latest_release(channel=channel), urls, None
    except Exception as exc:
        return None, urls, exc
    finally:
        updater.urllib.request.urlopen = orig


rel_stable, urls_stable, err_stable = fetch_stubbed("stable")
rel_master, urls_master, err_master = fetch_stubbed("master")
rel_default, urls_default, err_default = fetch_stubbed()

check("stable channel fetches releases/latest",
      urls_stable == [updater.API_LATEST_URL] and err_stable is None)
check("master channel fetches the same releases/latest URL",
      urls_master == [updater.API_LATEST_URL] and err_master is None)
check("omitted channel fetches releases/latest",
      urls_default == [updater.API_LATEST_URL] and err_default is None)
check("stable lookup returns the parsed release",
      rel_stable is not None and rel_stable.tag == "v1.2.3"
      and rel_stable.version == "1.2.3"
      and rel_stable.assets.get("NyaaTriggers-linux.tar.gz") == "https://example.test/dl")
check("master channel returns the same parsed release as stable",
      rel_master is not None and rel_master == rel_stable)

_, urls_es, err_es = fetch_stubbed("stable", ERROR_PAYLOAD)
_, urls_em, err_em = fetch_stubbed("master", ERROR_PAYLOAD)
check("API error payload raises on the stable channel",
      isinstance(err_es, ValueError) and urls_es == [updater.API_LATEST_URL])
check("API error payload raises on the master channel too",
      isinstance(err_em, ValueError) and urls_em == [updater.API_LATEST_URL])

check("_pick_master_release is gone", not hasattr(updater, "_pick_master_release"))


# Rate-limit mapping. The anonymous budget exhausted and a secondary limit
# both surface as RateLimited so the caller serves the cached release. A
# plain 403 with neither marker still propagates as a generic error.
def _http_error(code, headers):
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError(updater.API_LATEST_URL, code, "Forbidden", msg, None)


def fetch_error_stubbed(exc):
    """Run fetch_latest_release with urlopen raising exc. Returns the error."""
    def fake_urlopen(req, timeout=None):
        raise exc

    orig = updater.urllib.request.urlopen
    updater.urllib.request.urlopen = fake_urlopen
    try:
        updater.fetch_latest_release()
        return None
    except Exception as got:
        return got
    finally:
        updater.urllib.request.urlopen = orig


err = fetch_error_stubbed(_http_error(403, {"X-RateLimit-Remaining": "0"}))
check("403 with the budget exhausted maps to RateLimited",
      isinstance(err, updater.RateLimited))
err = fetch_error_stubbed(_http_error(403, {"Retry-After": "60"}))
check("403 with a Retry-After secondary limit maps to RateLimited",
      isinstance(err, updater.RateLimited))
err = fetch_error_stubbed(_http_error(403, {}))
check("plain 403 without rate markers still propagates",
      isinstance(err, urllib.error.HTTPError)
      and not isinstance(err, updater.RateLimited))


# The rolling scheme's run-numbered tag must sort above its own base stable.
check("rolling tag outranks its base stable",
      updater.parse_version("1.1.4.40-master") > updater.parse_version("1.1.4"))


# is_update_for_here: every install kind compares the full tag. Frozen builds
# carry the stamped rolling version. Git/source installs report the base
# version (repo _VERSION is never run-number-stamped), so a rolling tag on
# their base IS offered to them: the UI snoozes each offered tag so the
# per-push release stream does not nag, and a git checkout's Install pulls it
# past the tag.
check("git checkout sees a rolling tag on the same base as an update",
      updater.is_update_for_here("1.1.3.46", "1.1.3", kind="git"))
check("git checkout at the release's own version is up to date",
      not updater.is_update_for_here("1.1.3.46", "1.1.3.46", kind="git"))
check("source copy sees a rolling tag on the same base as an update",
      updater.is_update_for_here("1.1.3.46", "1.1.3", kind="source"))
check("source copy at the release's own version is up to date",
      not updater.is_update_for_here("1.1.3.46", "1.1.3.46", kind="source"))
check("source copy on a newer base than the release is up to date",
      not updater.is_update_for_here("1.1.3.46", "1.1.4", kind="source"))
check("git checkout on an older base still sees the update",
      updater.is_update_for_here("1.1.4.2", "1.1.3", kind="git"))
check("source copy on an older base still sees the update",
      updater.is_update_for_here("1.1.4.2", "1.1.3", kind="source"))
check("frozen install still compares the full rolling version",
      updater.is_update_for_here("1.1.3.46", "1.1.3.45", kind="frozen-linux"))
check("frozen install on the same rolling version is up to date",
      not updater.is_update_for_here("1.1.3.46", "1.1.3.46", kind="frozen-windows"))

# parse_version drops trailing zeros, so "1.2.0" == "1.2" and "1.2.0.51" sorts
# above both. Pin the comparisons every install kind now shares.
check("git checkout at 1.2.0 sees the 1.2.1 patch release",
      updater.is_update_for_here("1.2.1", "1.2.0", kind="git"))
check("source copy at 1.2.0 sees the 1.2.1 patch release",
      updater.is_update_for_here("1.2.1", "1.2.0", kind="source"))
check("1.2.0 is not re-offered 1.2.0",
      not updater.is_update_for_here("1.2.0", "1.2.0", kind="git"))
check("remote 1.2 vs current 1.2.0 reads as equal, not newer",
      not updater.is_update_for_here("1.2", "1.2.0", kind="git"))
check("a 2.0.0 install still sees 2.1",
      updater.is_update_for_here("2.1", "2.0.0", kind="git"))
check("rolling tag on a trailing-zero base updates a git checkout",
      updater.is_update_for_here("1.2.0.51", "1.2.0", kind="git"))
check("rolling tag on a trailing-zero base updates a source copy",
      updater.is_update_for_here("1.2.0.51", "1.2.0", kind="source"))
check("rolling tag on a newer base updates a trailing-zero install",
      updater.is_update_for_here("1.2.1.3", "1.2.0", kind="git"))
check("frozen install at 1.2.0 sees 1.2.1 (strict compare)",
      updater.is_update_for_here("1.2.1", "1.2.0", kind="frozen-linux"))


# display_version: frozen builds show the stamped _VERSION as-is; git
# checkouts show the nearest same-base rolling tag via git describe; plain
# source copies are marked so a screenshot can't pass for a release build.
check("frozen display is the stamped version",
      updater.display_version("1.3.0.175", kind="frozen-linux") == "1.3.0.175")
check("windows frozen display is the stamped version",
      updater.display_version("1.3.0.175", kind="frozen-windows") == "1.3.0.175")
check("source copy display is marked -src",
      updater.display_version("1.3.0", kind="source") == "1.3.0-src")
check("this git checkout's display stays on the 1.3.0 line",
      updater.display_version("1.3.0", kind="git").startswith("1.3.0"))

# _describe_label: exact tag, commits-past form, and the rejections.
check("exact rolling tag shows as the plain tag version",
      updater._describe_label("1.3.0", "v1.3.0.165") == "1.3.0.165")
check("commits past a rolling tag shows the plain tag",
      updater._describe_label("1.3.0", "v1.3.0.165-9-ga6f6aa27") == "1.3.0.165")
check("commits past a hand-cut base tag shows the plain base",
      updater._describe_label("1.3.0", "v1.3.0-3-gdeadbee") == "1.3.0")
check("trailing newline from describe is tolerated",
      updater._describe_label("1.3.0", "v1.3.0.165\n") == "1.3.0.165")
check("a tag from an older base line is rejected",
      updater._describe_label("1.3.0", "v1.2.7.141-40-gdeadbee") is None)
check("a tag from a newer base line is rejected",
      updater._describe_label("1.3.0", "v1.3.1.2") is None)
check("a non-version describe result is rejected",
      updater._describe_label("1.3.0", "deadbee") is None)

# git_covers_upstream: a checkout whose HEAD already contains the upstream
# tip, just pushed or ahead with unpushed commits, has nothing to pull, so
# rolling-tag offers stay quiet. A moved-on tip the clone has not fetched,
# or any git failure, answers False and the caller falls back to version math.
import subprocess
import tempfile
from pathlib import Path


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=15)


def _commit(repo, text, msg):
    (repo / "f.txt").write_text(text)
    _git(repo, "add", ".")
    return _git(repo, "commit", "-qm", msg)


with tempfile.TemporaryDirectory() as td:
    remote = Path(td) / "remote"
    clone = Path(td) / "clone"
    remote.mkdir()
    r = _git(remote, "init", "-q", "-b", "main")
    _git(remote, "config", "user.email", "t@t")
    _git(remote, "config", "user.name", "t")
    check("seed repo for upstream-cover test created",
          r.returncode == 0 and _commit(remote, "one", "one").returncode == 0)
    r = subprocess.run(["git", "clone", "-q", str(remote), str(clone)],
                       capture_output=True, text=True, timeout=15)
    check("clone for upstream-cover test created", r.returncode == 0)
    # The clone needs its own identity too. CI runners have no global one.
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    if r.returncode == 0:
        check("checkout level with the upstream tip is covered",
              updater.git_covers_upstream(clone))
        check("checkout ahead of upstream is covered",
              _commit(clone, "two", "two").returncode == 0
              and updater.git_covers_upstream(clone))
        # The tip object is unknown to the clone, the ancestor test errors.
        check("checkout behind upstream is not covered",
              _commit(remote, "three", "three").returncode == 0
              and not updater.git_covers_upstream(clone))
        check("a repo with no upstream is not covered",
              not updater.git_covers_upstream(remote))
        check("a non-repo path is not covered",
              not updater.git_covers_upstream(Path(td) / "nope"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests passed")
