"""Keep rolling releases ordered and preserve builds still in progress."""

import argparse
import json
import re
import sys


def version(tag):
    if not isinstance(tag, str) or not re.fullmatch(
            r"v[0-9]+(?:\.[0-9]+){2,3}(?:[-+][A-Za-z0-9.-]+)?", tag):
        return None
    # Match the leading digits per segment used by the program updater.
    parts = tuple(int(match[0]) if (match := re.match(r"[0-9]+", part)) else 0
                  for part in tag[1:].split("."))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    plain = all(part.isdigit() for part in tag[1:].split("."))
    return parts, plain


def published_versions(releases):
    return [parsed for release in releases
            if release.get("isDraft") is False and release.get("isPrerelease") is False
            and (parsed := version(release.get("tagName"))) is not None]


def superseded_tags(releases, keep):
    current = version(keep)
    if current is None:
        raise ValueError("Invalid release tag")
    return [release["tagName"] for release in releases
            if release.get("isDraft") is False and release.get("isPrerelease") is False
            and isinstance(release.get("tagName"), str)
            and re.fullmatch(r"v[0-9]+(?:\.[0-9]+){3}", release["tagName"])
            and version(release["tagName"]) < current]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "prune", "latest"))
    parser.add_argument("tag")
    args = parser.parse_args()
    releases = json.load(sys.stdin)
    if not isinstance(releases, list) or any(not isinstance(item, dict) for item in releases):
        raise ValueError("Invalid release listing")
    candidate = version(args.tag)
    if candidate is None:
        raise ValueError("Invalid release tag")
    # A publication retry must not treat its own completed release as newer.
    compared = ([release for release in releases if release.get("tagName") != args.tag]
                if args.action == "latest" else releases)
    published = published_versions(compared)
    newer = not published or candidate > max(published)
    if args.action == "check":
        if not newer:
            raise SystemExit(f"{args.tag} is not newer than the published stable releases")
    elif args.action == "latest":
        print("true" if newer else "false")
    else:
        for tag in superseded_tags(releases, args.tag):
            print(tag)


if __name__ == "__main__":
    main()
