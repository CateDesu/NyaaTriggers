import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("discord_notify", Path(__file__).with_name("discord_notify.py"))
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.image_failures = 0
        self.image = PNG
        self.post_status = 200
        self.metadata = '<meta content="/image.png?a=1&amp;b=2" property="og:image">'
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if self.path.startswith("/image.png"):
                    if test.image_failures:
                        test.image_failures -= 1
                        self.send_error(503)
                        return
                    body = test.image
                else:
                    body = test.metadata.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                test.post_path = self.path
                test.post = BytesParser(policy=default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                    + self.rfile.read(int(self.headers["Content-Length"]))
                )
                self.send_response(test.post_status)
                self.end_headers()
                self.wfile.write(b'{"id":"test-message"}')

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.env = {
            "REPO": "CateDesu/Kurisu", "REF": "main", "ACTOR": "CateDesu",
            "COMMIT_SHA": "a" * 40, "COMMIT_URL": self.url + "/commit/large",
            "COMMIT_MSG": "Update 11,161 lines\n\n- Keep the original GitHub card",
        }

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def run_workflow_post(self, webhook=None):
        workflow = Path(__file__).with_name("workflows") / "discord-notify.yml"
        post = workflow.read_text().split("      - name: Post push to Discord\n", 1)[1]
        script = textwrap.dedent(post.split("        run: |\n", 1)[1])
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, **self.env, "RUNNER_TEMP": str(self.output),
                 "DISCORD_WEBHOOK": self.url + "/webhook?thread_id=123" if webhook is None else webhook},
            check=True, capture_output=True,
        )

    def test_original_card_is_uploaded_in_the_webhook_multipart_body(self):
        self.run_workflow_post()
        fields = {part.get_param("name", header="content-disposition"): part for part in self.post.iter_parts()}
        payload = json.loads(fields["payload_json"].get_payload(decode=True))
        image = fields["files[0]"]
        self.assertEqual(image.get_payload(decode=True), PNG)
        self.assertEqual(image.get_filename(), payload["attachments"][0]["filename"])
        self.assertEqual(payload["embeds"][0]["image"]["url"], "attachment://" + image.get_filename())
        self.assertEqual(payload["embeds"][0]["url"], self.env["COMMIT_URL"])
        self.assertTrue(payload["content"].endswith("<" + self.env["COMMIT_URL"] + ">"))
        self.assertIn("/image.png?a=1&b=2", self.requests)
        self.assertEqual(self.post_path, "/webhook?thread_id=123&wait=true")

    def test_missing_secret_skips_network_requests(self):
        self.run_workflow_post(webhook="")
        self.assertEqual(self.requests, [])
        self.assertFalse(hasattr(self, "post"))

    def test_webhook_rejection_fails_the_workflow_step(self):
        self.post_status = 503
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_workflow_post(webhook=self.url + "/webhook")
        self.assertEqual(self.post_path, "/webhook?wait=true")

    def test_preview_generation_can_recover_before_posting(self):
        self.image_failures = 1
        with patch.object(notify.time, "sleep") as sleep:
            self.assertEqual(notify.fetch_preview(self.url + "/commit/large"), PNG)
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(path.startswith("/image.png") for path in self.requests), 2)

    def test_missing_preview_keeps_the_link_without_a_broken_attachment(self):
        self.metadata = "<html><head></head></html>"
        (self.output / "discord-preview.png").write_bytes(b"previous commit")
        with patch.object(notify.time, "sleep") as sleep, patch("builtins.print") as warning:
            notify.prepare(self.env, self.output)
        warning.assert_called_once()
        self.assertEqual(sleep.call_count, 3)
        payload = json.loads((self.output / "discord-notification.json").read_text())
        self.assertNotIn("embeds", payload)
        self.assertNotIn("attachments", payload)
        self.assertFalse((self.output / "discord-preview.png").exists())
        self.assertTrue(payload["content"].endswith(self.env["COMMIT_URL"]))

    def test_non_image_and_oversized_responses_are_not_attached(self):
        for body in [b"<html>Not ready</html>", PNG[:-12], PNG[:-12] + b"x" * (5 * 1024 * 1024) + PNG[-12:]]:
            with self.subTest(size=len(body)), patch("builtins.print"):
                self.image = body
                self.assertIsNone(notify.fetch_preview(self.url + "/commit/large", attempts=1))

    def test_long_commit_messages_keep_the_link_and_embed_within_limits(self):
        message = 'Large change "quoted" `$(touch nope)`\n' + "日本語\n" * 3000
        for repo in ["CateDesu/Kurisu", "CateDesu/NyaaTriggers"]:
            payload = notify.notification(repo, "main", "CateDesu", message, self.env["COMMIT_URL"], True)
            self.assertLessEqual(len(payload["content"]), 2000)
            self.assertLessEqual(len(payload["embeds"][0]["title"]), 256)
            self.assertEqual(payload["username"], repo.split("/")[1])
            self.assertIn('"quoted" `$(touch nope)`', payload["content"])
            self.assertTrue(payload["content"].endswith("<" + self.env["COMMIT_URL"] + ">"))

    def test_manual_run_uses_the_selected_commit_message(self):
        self.env["COMMIT_SHA"] = "HEAD"
        self.env["COMMIT_MSG"] = ""
        expected = subprocess.check_output(["git", "show", "-s", "--format=%s", "HEAD"], text=True).strip()
        notify.prepare(self.env, self.output)
        payload = json.loads((self.output / "discord-notification.json").read_text())
        self.assertEqual(payload["embeds"][0]["title"], expected)


if __name__ == "__main__":
    unittest.main()
