"""Offline integration checks: real SQLite/DPAPI/files and mocked TikTok HTTP.

No requests are sent to TikTok and no user files/database are modified.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, parse_qs
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import core
import publishing as pub
import tiktok_api as api

ORIGINAL_ROOT = core.ROOT
INFO = {"creator_username": "shop", "creator_nickname": "Магазин", "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
        "max_video_post_duration_sec": 60, "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False}


class PublishingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-publishing-")
        self.root = Path(self.temp.name)
        self.patch_root = patch.object(core, "ROOT", self.root)
        self.patch_db = patch.object(core, "DB", self.root / "data/studio.db")
        self.patch_root.start(); self.patch_db.start()
        core.initialize(); pub.initialize()
        (self.root / "static").mkdir()
        self.clock = 1000.0
        self.time_patch = patch.object(pub, "time", SimpleNamespace(time=lambda: self.clock))
        self.time_patch.start()
        self.calls, self.init_count = [], 0
        self.fail_init = False
        self.lost_init = False
        self.lost_upload = False
        self.remote_state = "PUBLISH_COMPLETE"
        self.factory = patch.object(pub, "_new_client", lambda row: api.TikTokClient(transport=httpx.MockTransport(self.http)))
        self.factory.start()
        self.accounts = []
        for n in range(2):
            account_id = pub.save_account({"label": f"Магазин {n+1}", "proxy": "http://login:secret@127.0.0.1:8888"})["id"]
            pub._save_tokens(account_id, {"open_id": f"open-{n}", "access_token": f"token-{n}", "refresh_token": "refresh", "scope": "video.publish", "expires_in": 86400})
            pub._save_creator(account_id, INFO)
            self.accounts.append(account_id)
        pub.save_configuration({"client_key": "test-key", "client_secret": "test-secret"})
        self.media = []
        # The queue tests byte identity and transport, not a video codec.
        for n in range(3):
            path = self.root / f"ролик {n}.mp4"
            path.write_bytes(bytes([n+1])*1024)
            with patch.object(core, "probe", return_value={"video": True, "duration": 13}):
                self.media.append(pub.add_media(path, caption="Мой магазин #один"))
        self.publisher = pub.Publisher()
        core.set_meta("publishing_paused", False)

    def tearDown(self):
        self.publisher.stop.set()
        self.factory.stop(); self.time_patch.stop(); self.patch_db.stop(); self.patch_root.stop()
        self.temp.cleanup()

    def http(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else None
        self.calls.append((request.method, path, body, request.headers.get("authorization")))
        if path == "/v2/oauth/token/":
            return httpx.Response(200, json={"access_token": "rotated", "refresh_token": "rotated-refresh", "expires_in": 86400, "open_id": "open-0", "scope": "video.publish"})
        if path == "/v2/post/publish/creator_info/query/":
            return httpx.Response(200, json={"data": INFO, "error": {"code": "ok"}})
        if path == "/v2/post/publish/video/init/":
            self.init_count += 1
            if self.lost_init:
                raise httpx.ReadTimeout("simulated lost response", request=request)
            if self.fail_init:
                return httpx.Response(400, json={"error": {"code": "privacy_level_option_mismatch"}})
            self.assertEqual(body["source_info"]["total_chunk_count"], 1)
            self.assertEqual(body["post_info"]["privacy_level"], "SELF_ONLY")
            return httpx.Response(200, json={"data": {"publish_id": f"pub-{self.init_count}", "upload_url": "https://open-upload.tiktokapis.com/video/?upload_token=secret"}, "error": {"code": "ok"}})
        if path == "/video/":
            self.assertEqual(request.headers["content-range"], "bytes 0-1023/1024")
            self.assertIsNone(request.headers.get("authorization"))
            if self.lost_upload:
                raise httpx.ReadTimeout("simulated lost upload response", request=request)
            return httpx.Response(201)
        if path == "/v2/post/publish/status/fetch/":
            return httpx.Response(200, json={"data": {"status": self.remote_state, "publicaly_available_post_id": ["123"]}, "error": {"code": "ok"}})
        raise AssertionError("Unexpected network request: " + path)

    def assignment(self, media=0, account=0, **extra):
        return {"media_id": self.media[media], "account_id": self.accounts[account], "privacy": "SELF_ONLY", **extra}

    def rows(self):
        with core.database() as db:
            return [dict(x) for x in db.execute("SELECT * FROM publishing_tasks ORDER BY created")]

    def test_interval_and_correct_account_and_confirmation(self):
        pub.enqueue([self.assignment(0, 0), self.assignment(1, 1)])
        self.publisher.step()
        self.assertEqual(self.init_count, 1)
        self.assertEqual([x["status"] for x in self.rows()], ["processing", "queued"])
        self.clock = 1039
        self.publisher.step()
        self.assertEqual(self.init_count, 1)
        self.assertEqual(self.rows()[0]["status"], "published")
        self.clock = 1040
        self.publisher.step()
        self.assertEqual(self.init_count, 2)
        inits = [x for x in self.calls if x[1].endswith("/video/init/")]
        self.assertEqual([x[3] for x in inits], ["Bearer token-0", "Bearer token-1"])

    def test_renamed_copy_and_other_account_cannot_duplicate(self):
        path = self.root / "переименован.mp4"
        path.write_bytes((self.root / "ролик 0.mp4").read_bytes())
        with patch.object(core, "probe", return_value={"video": True, "duration": 13}):
            self.assertEqual(pub.add_media(path), self.media[0])
        pub.enqueue([self.assignment()])
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment(0, 1)])
        self.assertEqual(len(self.rows()), 1)

    def test_batch_is_atomic_on_one_duplicate(self):
        pub.enqueue([self.assignment()])
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment(1), self.assignment(0)])
        self.assertEqual(len(self.rows()), 1)

    def enable_quick(self):
        for account_id in self.accounts:
            pub.save_account({"id": account_id, "label": pub.account(account_id)["label"],
                              "quick": {"enabled": True, "privacy": "SELF_ONLY", "brand": "own"}})

    def test_quick_start_assigns_new_videos_once_and_persists_settings(self):
        self.enable_quick()
        with patch.object(pub, "sync_generated") as sync:
            result = pub.quick_start()
            self.assertEqual(result["queued"], 3)
            sync.assert_called_once()
        self.assertEqual([x["account_id"] for x in self.rows()], [self.accounts[0], self.accounts[1], self.accounts[0]])
        self.assertTrue(all(json.loads(x["post_info"])["brand_organic_toggle"] for x in self.rows()))
        core.set_meta("publishing_paused", True)
        result = pub.quick_start()
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["resumed"], 3)
        self.assertEqual(len(self.rows()), 3)
        self.assertFalse(core.get_meta("publishing_paused"))
        self.assertTrue(pub.public_state()["accounts"][0]["quick"]["enabled"])

    def test_quick_start_skips_used_and_missing_files(self):
        self.enable_quick()
        pub.enqueue([self.assignment(0)])
        pub.update_task(self.rows()[0]["id"], status="published")
        (self.root / "ролик 1.mp4").unlink()
        result = pub.quick_start()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.rows()[-1]["media_id"], self.media[2])

    def test_quick_start_requires_explicit_saved_privacy_and_stops_for_disconnected_recipient(self):
        with self.assertRaises(ValueError):
            pub.quick_start()
        self.enable_quick()
        pub.disconnect(self.accounts[0])
        with self.assertRaises(ValueError):
            pub.quick_start()
        self.assertEqual(self.rows(), [])

    def test_simultaneous_quick_clicks_do_not_duplicate(self):
        self.enable_quick()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _:pub.quick_start(), range(2)))
        self.assertEqual(sum(x["queued"] for x in results), 3)
        self.assertEqual(len(self.rows()), 3)

    def test_simultaneous_enqueue_only_reserves_once(self):
        def add(account_index):
            try:
                pub.enqueue([self.assignment(0, account_index)])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(add, [0, 1]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_pause_stops_new_upload_but_confirms_existing(self):
        pub.enqueue([self.assignment(0), self.assignment(1)])
        self.publisher.step()
        core.set_meta("publishing_paused", True)
        self.clock += 60
        self.publisher.step()
        self.assertEqual(self.init_count, 1)
        self.assertEqual(self.rows()[0]["status"], "published")
        self.assertEqual(self.rows()[1]["status"], "queued")

    def test_lost_init_response_is_never_automatically_retried(self):
        self.lost_init = True
        pub.enqueue([self.assignment()]); self.publisher.step()
        self.assertEqual(self.rows()[0]["status"], "uncertain")
        self.clock += 100
        self.publisher.step()
        self.assertEqual(self.init_count, 1)
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment(0, 1)])

    def test_lost_upload_response_reconciles_existing_id(self):
        self.lost_upload = True
        pub.enqueue([self.assignment()]); self.publisher.step()
        self.assertEqual(self.rows()[0]["status"], "processing")
        self.clock += 20; self.publisher.step()
        self.assertEqual(self.rows()[0]["status"], "published")
        self.assertEqual(self.init_count, 1)

    def test_failed_init_can_retry_after_definitive_rejection(self):
        self.fail_init = True
        pub.enqueue([self.assignment()]); self.publisher.step()
        task = self.rows()[0]
        self.assertEqual(task["status"], "failed")
        self.fail_init = False
        pub.task_action(task["id"], "retry")
        self.clock += 40; self.publisher.step()
        self.assertEqual(self.rows()[0]["status"], "processing")

    def test_cancel_and_retry_cannot_bypass_new_reservation(self):
        pub.enqueue([self.assignment()])
        task = self.rows()[0]
        pub.task_action(task["id"], "cancel")
        pub.enqueue([self.assignment(0, 1)])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1]["status"], "queued")

    def test_restart_preserves_history_and_pauses_new_work(self):
        pub.enqueue([self.assignment(0), self.assignment(1)])
        first = self.rows()[0]
        pub.update_task(first["id"], status="uploading", publish_id="existing-id", started=self.clock)
        with patch.object(self.publisher, "run", return_value=None):
            self.publisher.start(); self.publisher.close()
        self.assertEqual([x["status"] for x in self.rows()], ["processing", "queued"])
        self.assertTrue(core.get_meta("publishing_paused"))

    def test_secrets_are_encrypted_and_redacted_from_state(self):
        row = pub.account(self.accounts[0])
        self.assertNotIn("token-0", row["secret"])
        self.assertNotIn("login", row["proxy"])
        public = json.dumps(pub.public_state())
        for secret in ("token-0", "refresh_token", "test-secret", "login:secret"):
            self.assertNotIn(secret, public)
        self.assertEqual(api.unprotect(row["secret"])["access_token"], "token-0")

    def test_oauth_cookie_pkce_and_token_rotation(self):
        url, cookie = pub.oauth_start(self.accounts[0])
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(len(query["code_challenge"][0]), 64)
        with self.assertRaises(ValueError):
            pub.oauth_finish(query["state"][0], "code", "wrong-cookie")
        pub.oauth_finish(query["state"][0], "code", cookie)
        saved = api.unprotect(pub.account(self.accounts[0])["secret"])
        self.assertEqual(saved["access_token"], "rotated")
        with self.assertRaises(ValueError):
            pub.oauth_finish(query["state"][0], "code", cookie)

    def test_expired_token_refresh_uses_rotated_credentials(self):
        row = pub.account(self.accounts[0])
        values = api.unprotect(row["secret"]); values["expires_at"] = 0
        with core.database() as db:
            db.execute("UPDATE publishing_accounts SET secret=? WHERE id=?", (api.protect(values), row["id"]))
        client, token = pub.authorized_client(row["id"]); client.close()
        self.assertEqual(token, "rotated")
        self.assertEqual(api.unprotect(pub.account(row["id"])["secret"])["refresh_token"], "rotated-refresh")

    def test_changed_video_and_unavailable_permission_are_rejected(self):
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment(allow_duet=True)])
        (self.root / "ролик 0.mp4").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment()])

    def test_chunk_boundaries_and_transport_proxy(self):
        self.assertEqual(api.chunk_plan(4_000_000), (4_000_000, 1))
        self.assertEqual(api.chunk_plan(50_000_123), (10_000_000, 5))
        self.assertEqual(api.chunk_plan(64_000_001), (10_000_000, 6))
        with self.assertRaises(ValueError):
            api.validate_proxy("file:///C:/private")
        with patch.object(api.httpx, "Client") as factory:
            client = api.TikTokClient("socks5://127.0.0.1:1080"); client.close()
            self.assertEqual(factory.call_args.kwargs["proxy"], "socks5://127.0.0.1:1080")
            self.assertFalse(factory.call_args.kwargs["trust_env"])

    def test_real_chunk_upload_ranges_and_final_remainder(self):
        path = self.root / "chunks.mp4"
        path.write_bytes(b"x" * 20_000_123)
        ranges = []
        def receive(request):
            ranges.append((request.headers["content-range"], len(request.content)))
            return httpx.Response(206 if len(ranges) == 1 else 201)
        client = api.TikTokClient(transport=httpx.MockTransport(receive))
        progress = []
        try:
            client.upload("https://open-upload.tiktokapis.com/video/?upload_token=secret", path, 10_000_000, 2, progress.append, lambda:False)
        finally:
            client.close()
        self.assertEqual(ranges, [("bytes 0-9999999/20000123", 10_000_000), ("bytes 10000000-20000122/20000123", 10_000_123)])
        self.assertEqual(progress[-1], 100)

    def test_local_api_rejects_wrong_origin_and_missing_setup(self):
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        response = client.post("/api/publishing/pause", json={}, headers={"Origin": "https://foreign.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/publishing/state").status_code, 200)
        response = client.post("/api/publishing/media/"+self.media[0]+"/already-posted", json={"account_id": self.accounts[0]})
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(ValueError):
            pub.enqueue([self.assignment(0, 1)])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PublishingChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"checks": result.testsRun, "passed": result.wasSuccessful(), "failures": len(result.failures), "errors": len(result.errors),
              "network": "httpx.MockTransport only; no real TikTok calls", "scope": "queue, deduplication, cooldown, OAuth/DPAPI, recovery, API transport"}
    (ORIGINAL_ROOT / "docs/publishing-check-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
