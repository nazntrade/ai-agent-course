"""Independent-review coverage: actual auth streams, admission and lazy startup."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app.config import Settings
from gateway.app.database import DomainError, Store
from gateway.app.main import create_app
from gateway.app.upstream import StreamResult
from gateway.app.worker import StubProvider
from gateway.app.manage import provision_device
from gateway.tests.test_gateway import wait_job

pytestmark = pytest.mark.integration


def test_concurrent_pairing_never_deletes_winning_payload(tmp_path):
    store=Store(tmp_path/"chat.sqlite3",Settings(tmp_path),clock=lambda:1000)
    directory=tmp_path/"pairing"
    entered=threading.Event()
    release=threading.Event()
    ordering=threading.Lock()
    calls=[]
    def protector(payload):
        with ordering:
            index=len(calls)
            calls.append(index)
        if index==0:
            entered.set()
            assert release.wait(3)
        return payload
    def provision(_):
        try:
            return provision_device(store,directory,"owner-a","race",protector=protector,
                                    restrict=lambda folder:folder.mkdir(exist_ok=True))
        except (FileExistsError,ValueError):
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(provision,0)
            assert entered.wait(3)
            second=executor.submit(provision,1)
            try:
                time.sleep(0.1)
                assert not second.done() and calls==[0]
            finally:
                release.set()
            results=[first.result(),second.result()]
        winner=[result for result in results if result is not None]
        assert len(winner)==1
        assert (directory/"initial-device.dpapi").is_file()
        assert json.loads((directory/"pending.json").read_text())["device_id"]==winner[0]["device_id"]
        assert store.db.execute("SELECT count(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0]==1
        assert store.db.execute("SELECT count(*) FROM devices").fetchone()[0]==1
    finally:
        store.close()


def test_actual_http_rate_retry_after_and_replay_never_spends_or_acquires(tmp_path):
    settings = Settings(tmp_path)
    store = Store(tmp_path / "chat.sqlite3", settings, clock=lambda: 1000)
    _, token = store.provision("owner-a", "test", pairing=False)
    headers = {"Authorization": "Bearer " + token}
    class CountPool:
        manager = None
        gets = 0
        async def get(self):
            self.gets += 1
            return StubProvider()
        async def close(self):
            pass
    pool = CountPool()
    with TestClient(create_app(settings, store=store, pool=pool)) as client:
        assert pool.gets == 0
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/status", headers=headers).status_code == 200
        assert pool.gets == 0
        jobs = []
        for index in range(2):
            conversation = client.post("/v1/conversations", json={"title": str(index)}, headers=headers).json()
            body = {"text": "hello", "expected_revision": 1}
            h = dict(headers, **{"Idempotency-Key": f"key-{index:016d}"})
            admitted = client.post("/v1/conversations/" + conversation["id"] + "/requests", json=body, headers=h)
            assert admitted.status_code == 202
            done = wait_job(client, headers, admitted.json()["id"])
            assert done["state"] == "completed"
            jobs.append((conversation, body, h, done))
        assert pool.gets == 2
        conversation = client.post("/v1/conversations", json={"title": "rate"}, headers=headers).json()
        exhausted = client.post("/v1/conversations/" + conversation["id"] + "/requests",
            json={"text": "hello", "expected_revision": 1}, headers=dict(headers, **{"Idempotency-Key": "key-third-abcdef"}))
        assert exhausted.status_code == 429 and exhausted.headers["Retry-After"] == "12"
        assert exhausted.json() == {"error": {"code": "rate_limited"}}
        first, body, h, done = jobs[0]
        replay = client.post("/v1/conversations/" + first["id"] + "/requests", json=body, headers=h)
        assert replay.status_code == 200 and replay.json()["id"] == done["id"]
        for _ in range(2):
            assert client.get("/healthz").status_code == 200
            assert client.get("/v1/requests/" + done["id"] + "/snapshot", headers=headers).status_code == 200
            assert client.get("/v1/requests/" + done["id"] + "/events", headers=headers).status_code == 200
            assert client.get("/v1/requests/by-key/key-0000000000000000", headers=headers).status_code == 200
        assert pool.gets == 2
        assert store.counts() == {"waiting_jobs": 0, "active_jobs": 0}


def test_global_eight_waiting_last_slot_race_and_cancel_frees_slot(tmp_path):
    store = Store(tmp_path / "chat.sqlite3", Settings(tmp_path), clock=lambda: 1000)
    try:
        queued = []
        for index in range(7):
            _, token = store.provision("owner-" + str(index), "test", pairing=False)
            identity = store.authenticate(token)
            conversation = store.create_conversation(identity["owner_id"], "queued")
            queued.append(store.admit(identity, conversation["id"], f"key-{index:016d}", "hello", 1)[0])
        candidates = []
        for index in range(2):
            _, token = store.provision("racer-" + str(index), "test", pairing=False)
            identity = store.authenticate(token)
            candidates.append((identity, store.create_conversation(identity["owner_id"], "last slot")))
        barrier = threading.Barrier(2)
        def racing(index):
            identity, conversation = candidates[index]
            barrier.wait(timeout=3)
            try:
                job, created = store.admit(identity, conversation["id"], f"racing-key-{index:016d}", "hello", 1)
                return index, job, created
            except DomainError as error:
                assert error.code == "queue_full" and error.status == 429 and error.retry_after == 1
                return index, None, False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(racing, range(2)))
        winners = [item for item in results if item[1] is not None]
        assert len(winners) == 1 and store.counts()["waiting_jobs"] == 8
        index, job, _ = winners[0]
        store.cancel(candidates[index][0]["owner_id"], job["id"])
        assert store.counts()["waiting_jobs"] == 7
        loser = 1 - index
        identity, conversation = candidates[loser]
        assert store.admit(identity, conversation["id"], f"racing-key-{loser:016d}", "hello", 1)[1]
        assert store.counts()["waiting_jobs"] == 8
        assert store.claim()["id"] == queued[0]["id"]
    finally:
        store.close()


async def test_genuinely_open_http_sse_receives_partials_then_revocation_closes(tmp_path):
    settings = Settings(tmp_path)
    store = Store(tmp_path / "chat.sqlite3", settings)
    device, token = store.provision("owner-a", "test", pairing=False)
    headers = {"Authorization": "Bearer " + token}
    second = asyncio.Event()
    first_seen = asyncio.Event()
    second_seen = asyncio.Event()
    class OpenProvider(StubProvider):
        async def stream(self, body, on_content=None):
            await on_content("first ")
            await second.wait()
            await asyncio.sleep(0.11)
            await on_content("second")
            await asyncio.Event().wait()
            raise AssertionError("Revocation test must not finish generation")
    class OpenPool:
        manager = None
        gets = 0
        async def get(self):
            self.gets += 1
            return OpenProvider()
        async def close(self):
            pass
    pool = OpenPool()
    app = create_app(settings, store=store, pool=pool)
    chunks = []
    starts = []
    async with app.router.lifespan_context(app):
        assert pool.gets == 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            conversation = (await client.post("/v1/conversations", json={"title": "stream"}, headers=headers)).json()
            admitted = (await client.post("/v1/conversations/" + conversation["id"] + "/requests",
                json={"text": "hello", "expected_revision": 1}, headers=dict(headers, **{"Idempotency-Key": "key-abcdefghijkl"}))).json()
            identifier = admitted["id"]
            received_request = False
            async def receive():
                nonlocal received_request
                if not received_request:
                    received_request = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
            async def send(message):
                if message["type"] == "http.response.start":
                    starts.append(message)
                elif message["type"] == "http.response.body" and message.get("body"):
                    text = message["body"].decode()
                    chunks.append(text)
                    for line in text.splitlines():
                        if line.startswith("data: "):
                            snapshot = json.loads(line[6:])
                            if snapshot["partial_content"] == "first ":
                                first_seen.set()
                            if snapshot["partial_content"] == "first second":
                                second_seen.set()
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": "/v1/requests/" + identifier + "/events",
                     "raw_path": ("/v1/requests/" + identifier + "/events").encode(), "query_string": b"",
                     "root_path": "", "headers": [(b"authorization", headers["Authorization"].encode())],
                     "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8791)}
            stream = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(first_seen.wait(), 3)
                assert not stream.done() and store.job("owner-a", identifier)["state"] == "running"
                second.set()
                await asyncio.wait_for(second_seen.wait(), 3)
                assert not stream.done() and pool.gets == 1
                store.revoke(device)
                await asyncio.wait_for(stream, 2)
                assert starts[0]["status"] == 200
                assert dict(starts[0]["headers"])[b"content-type"].startswith(b"text/event-stream")
                assert "first second" in "".join(chunks) and "event: terminal" not in "".join(chunks)
                assert (await client.get("/v1/requests/" + identifier, headers=headers)).status_code == 401
                assert pool.gets == 1
            finally:
                if not stream.done():
                    stream.cancel()
                    await asyncio.gather(stream, return_exceptions=True)
