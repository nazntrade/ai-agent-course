"""Measure exact count, real streamed output and borrowed-safe release."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from gateway.app.upstream import LeaseManager, LocalProvider, ProviderConfig, UpstreamError, chat_body
from harness.runner import ROOT, save_json


async def main(*, operation_deadline: float = 285) -> int:
    started = time.monotonic()
    measurements: dict = {}
    error_code = None
    release = {"status": "NOT_ASSESSED"}
    renewed = {"status": "NOT_ASSESSED"}
    manager = None
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        try:
            config = ProviderConfig.from_environment()
            manager = LeaseManager(config, client)
            async with asyncio.timeout(operation_deadline):
                await manager.ensure()
                provider = LocalProvider(config, client, manager)
                body = chat_body([
                    {"role": "system", "content": "You are a concise text assistant. Follow the user instruction."},
                    {"role": "user", "content": "Reply with exactly the single word READY."}], output_tokens=32)
                counted = await provider.count(body)
                if counted["context_capacity"] < 8192:
                    raise UpstreamError("actual_context_below_target")
                result = await provider.stream(body)
                measurements = {"input_tokens": counted["input_tokens"],
                    "actual_slot_capacity": counted["context_capacity"], "count_method": counted["method"],
                    "context_source": counted["context_source"], "stream_prompt_tokens": result.prompt_tokens,
                    "count_matches_inference": result.prompt_tokens == counted["input_tokens"],
                    "finish_reason": result.finish_reason, "done_received": result.done,
                    "content_chunks": result.content_chunks, "answer_characters": len(result.content),
                    "public_fixture_answer": result.content,
                    "answer_matches_instruction": result.content.strip().strip(".") == "READY",
                    "reasoning_characters_excluded": result.reasoning_characters,
                    "thinking_disabled": body["chat_template_kwargs"]["enable_thinking"] is False}
                if not measurements["count_matches_inference"]:
                    raise UpstreamError("native_count_inference_mismatch")
                if not measurements["answer_matches_instruction"]:
                    raise UpstreamError("preflight_answer_instruction_mismatch")
                renewed = await manager.renew()
        except TimeoutError:
            error_code = "preflight_deadline_exceeded"
        except UpstreamError as error:
            error_code = error.code
        except asyncio.CancelledError:
            error_code = "preflight_cancelled"
        except Exception:
            error_code = "preflight_execution_error"
        finally:
            if manager:
                try:
                    # close() shields its own bounded acquire/release tasks.
                    # Its release transport is bounded to 40 seconds; a late
                    # acquire retains its own 280-second bound. Never detach it.
                    release = await manager.close()
                except Exception:
                    error_code = error_code or "lease_release_failed"
                    release = {"status": "FAIL"}
    technical = "PASS" if error_code is None else "FAIL"
    common = {"schema_version": "preflight-evidence-v1", "scope": "P01 real local provider boundary",
              "run_id": os.environ.get("APP_RUN_ID", ""),
              "technical_status": technical, "acceptance_status": "PARTIAL", "error_code": error_code,
              "duration_seconds": round(time.monotonic() - started, 3),
              "configuration": {"kind": "local", "live_policy": "allowed", "redirects": False,
                                "environment_proxy": False}, "measurements": measurements}
    for criterion, filename, pending in [
        ("C01", "model-verification.json", ["complete application integration", "independent acceptance"]),
        ("C07", "context-verification.json", ["exact-budget boundary/+1", "history trimming", "independent acceptance"]),
        ("C12", "lifecycle-verification.json", ["controlled lifecycle failure", "borrowed model observation",
                                              "service restart/cancel/shutdown", "independent acceptance"])]:
        value = dict(common, criterion=criterion, remaining_checks=pending)
        if criterion == "C12":
            value.update(renew=renewed, release=release)
        save_json(ROOT / "docs/artifacts" / filename, value)
    print("MODEL_PREFLIGHT_STATUS: " + technical + (" (" + error_code + ")" if error_code else ""))
    return 0 if error_code is None else 1
