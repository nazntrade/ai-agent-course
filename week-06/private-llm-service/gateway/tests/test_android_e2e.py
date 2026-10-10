"""Fixed E2E faults and interrupted socket shutdown, without actual credentials/model."""
import asyncio
import json
import socket
import struct

import httpx
import pytest

from harness.android_gateway import FaultRelay
from harness.pairing_bridge import receive_ui_frame

pytestmark=pytest.mark.integration


@pytest.mark.asyncio
async def test_controlled_rate_is_one_response_then_restores_actual_gateway(tmp_path):
    relay=FaultRelay("http://127.0.0.1:1",tmp_path)
    relay.control("arm_rate")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app),base_url="http://fixture") as client:
        response=await client.post("/v1/conversations/fixture/requests",json={"text":"public fixture"})
        assert response.status_code==429 and response.headers["Retry-After"]=="2"
        assert relay.posts==0 and relay.rate_responses==1 and relay.rate is False
    with pytest.raises(ValueError): relay.control("http://foreign.invalid")


@pytest.mark.asyncio
async def test_cancelled_fixture_rejects_before_forwarding(tmp_path):
    (tmp_path/"CANCEL").write_text("cancel")
    relay=FaultRelay("http://127.0.0.1:1",tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app),base_url="http://fixture") as client:
        response=await client.get("/v1/me")
        assert response.status_code==404
        assert relay.posts==0


def test_authenticated_frame_partial_timeout_is_retained_without_replay():
    class Fragmented:
        def settimeout(self,value): pass
        def recv(self,count):
            value=self.parts.pop(0)
            if isinstance(value,Exception): raise value
            return value
    payload=json.dumps({"kind":"control","action":"job_count"}).encode()
    frame=struct.pack("!I",len(payload))+payload
    source=Fragmented(); source.parts=[frame[:2],socket.timeout(),frame[2:4],frame[4:]]
    assert receive_ui_frame(source,lambda:False)=={"kind":"control","action":"job_count"}
    with pytest.raises(ValueError,match="cancelled"):
        receive_ui_frame(source,lambda:True)
