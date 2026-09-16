"""Run release policy steps against a local GitHub CLI fixture."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github/workflows/release.yml").read_text()


def step_script(name):
    tail = WORKFLOW.split("- name: " + name, 1)[1]
    tail = tail.split("        run: |\n", 1)[1]
    lines = []
    for line in tail.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").symlink_to(REPO / "tools", target_is_directory=True)
        self.gh = self.root / "gh"
        self.gh.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["release", "list"] or args[0] == "api":
    if os.environ.get("FAIL_LIST"):
        sys.exit(1)
    data = json.loads(os.environ["RELEASE_FIXTURE"])
    if args[0] == "api":
        if os.environ.get("RELEASE_HTTP_URL"):
            args = [os.environ["RELEASE_HTTP_URL"] if arg.startswith("repos/") else arg for arg in args]
            sys.exit(subprocess.run([os.environ["REAL_GH"], *args], env={
                "PATH": os.environ["PATH"], "GH_TOKEN": "local-test-token"}).returncode)
        if "--paginate" not in args:
            data = data[:100]
        pages = [[{"tag_name": release["tagName"], "draft": release["isDraft"],
                   "prerelease": release["isPrerelease"]} for release in data]]
        print(json.dumps(pages))
    elif "--jq" in args:
        for release in data:
            print(release["tagName"])
    else:
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 30
        print(json.dumps(data[:limit]))
else:
    with Path("commands.jsonl").open("a") as out:
        out.write(json.dumps(args) + "\\n")
''')
        self.gh.chmod(0o755)

    def run_step(self, name, releases, **extra):
        env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
               "RELEASE_FIXTURE": json.dumps(releases), "TAG": "v1.4.0.100",
               "KEEP": "v1.4.0.100", "SUFFIX": "", "PRE": "false", **extra}
        script = step_script(name).replace("${{ github.repository }}", "test/program")
        result = subprocess.run(["bash", "-eo", "pipefail", "-c", script],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=5)
        log = self.root / "commands.jsonl"
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, commands

    def test_release_listing_failure_stops_rolling_build(self):
        result, commands = self.run_step("Refuse to regress the rolling channel", [], FAIL_LIST="1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(commands, [])

    def test_pruning_preserves_newer_releases_drafts_and_milestones(self):
        releases = [
            {"tagName": "v1.4.0.99", "isDraft": False, "isPrerelease": False},
            {"tagName": "v1.4.0.100", "isDraft": False, "isPrerelease": False},
            {"tagName": "v1.4.0.101", "isDraft": True, "isPrerelease": False},
            {"tagName": "v1.4.0.102", "isDraft": False, "isPrerelease": False},
            {"tagName": "v1.4.0", "isDraft": False, "isPrerelease": False},
        ]
        result, commands = self.run_step("Delete superseded rolling releases", releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        deleted = [args[2] for args in commands if args[:2] == ["release", "delete"]]
        self.assertEqual(deleted, ["v1.4.0.99"])

    def test_older_tag_does_not_replace_latest_release(self):
        releases = [{"tagName": "v1.4.0.102", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases, TAG="v1.4.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=false", commands[0])

    def test_newer_tag_becomes_latest(self):
        releases = [{"tagName": "v1.4.0.99", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=true", commands[0])

    def test_drafts_cannot_hide_the_latest_published_version(self):
        releases = [{"tagName": f"v1.4.0.{200 + index}", "isDraft": True,
                     "isPrerelease": False} for index in range(100)]
        releases.append({"tagName": "v1.4.0.102", "isDraft": False, "isPrerelease": False})
        result, commands = self.run_step("Refuse to regress the rolling channel", releases)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(commands, [])

    def test_publication_checks_releases_beyond_the_first_page(self):
        releases = [{"tagName": f"v1.4.0.{200 + index}", "isDraft": True,
                     "isPrerelease": False} for index in range(100)]
        releases.append({"tagName": "v1.4.0.102", "isDraft": False, "isPrerelease": False})
        result, commands = self.run_step("Publish release (un-draft)", releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=false", commands[0])

    def test_suffix_tag_keeps_the_existing_publication_path(self):
        releases = [{"tagName": "v1.4.0", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases, TAG="v1.5.0-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=true", commands[0])

    def test_suffix_versions_follow_the_program_comparison(self):
        releases = [{"tagName": "v1.5.0-test.2", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases, TAG="v1.5.0-test.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=true", commands[0])

    def test_final_release_supersedes_its_suffix_release(self):
        releases = [{"tagName": "v1.5.0-test", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases, TAG="v1.5.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=true", commands[0])

    def test_publication_retry_keeps_the_newest_release_latest(self):
        releases = [{"tagName": "v1.4.0.100", "isDraft": False, "isPrerelease": False}]
        result, commands = self.run_step("Publish release (un-draft)", releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=true", commands[0])

    def test_retry_of_an_older_release_cannot_take_latest(self):
        releases = [{"tagName": tag, "isDraft": False, "isPrerelease": False}
                    for tag in ("v1.4.0.100", "v1.4.0.101")]
        result, commands = self.run_step("Publish release (un-draft)", releases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--latest=false", commands[0])

    def test_release_order_agrees_with_the_program(self):
        from nyaatriggers import updater
        from tools.release_policy import version
        tags = ("v1.4.0", "v1.4.0.0", "v1.4.0.1", "v1.5.0-test", "v1.5.0",
                "v1.5.0-test.2", "v1.5.0-test.3", "v1.5.0.3", "v1.5.0+build")
        for remote in tags:
            for current in tags:
                with self.subTest(remote=remote, current=current):
                    self.assertEqual(version(remote) > version(current),
                                     updater.is_newer(remote, current))

    @unittest.skipUnless(shutil.which("gh"), "Transport check needs the GitHub CLI")
    def test_real_cli_reads_both_pages_before_publication(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                if "page=2" not in self.path:
                    self.send_header("Link", f'<http://127.0.0.1:{self.server.server_port}/releases?page=2>; rel="next"')
                    body = [{"tag_name": f"v1.4.0.{index + 200}", "draft": True,
                             "prerelease": False} for index in range(100)]
                else:
                    body = [{"tag_name": "v1.4.0.102", "draft": False, "prerelease": False}]
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result, commands = self.run_step("Publish release (un-draft)", [],
                RELEASE_HTTP_URL=f"http://127.0.0.1:{server.server_port}/releases?per_page=100",
                REAL_GH=shutil.which("gh"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2, requests)
        self.assertIn("--latest=false", commands[0])

    @unittest.skipUnless(shutil.which("gh"), "Transport check needs the GitHub CLI")
    def test_real_cli_later_page_failure_prevents_publication(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if "page=2" in self.path:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"message":"unavailable"}')
                    return
                self.send_response(200)
                self.send_header("Link", f'<http://127.0.0.1:{self.server.server_port}/releases?page=2>; rel="next"')
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'[{"tag_name":"v1.4.0.99","draft":false,"prerelease":false}]')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result, commands = self.run_step("Publish release (un-draft)", [],
                RELEASE_HTTP_URL=f"http://127.0.0.1:{server.server_port}/releases?per_page=100",
                REAL_GH=shutil.which("gh"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2, requests)
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
