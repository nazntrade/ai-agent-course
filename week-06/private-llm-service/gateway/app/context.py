"""Exact context budgeting keeps a contiguous suffix of completed pairs."""
import json
from gateway.app.upstream import UpstreamError, chat_body

SYSTEM = "You are a helpful private text assistant. Answer the user's request clearly and concisely."


async def prepare_context(provider, settings, current_text, history):
    base = [{"role":"system","content":SYSTEM},{"role":"user","content":current_text}]
    minimal = chat_body(base, output_tokens=settings.output_cap)
    counted = await provider.count(minimal)
    if counted["context_capacity"] < 8192:
        raise UpstreamError("actual_context_below_target")
    budget = min(settings.prompt_cap, counted["context_capacity"]-settings.output_cap-settings.safety_tokens)
    if counted["input_tokens"] > budget:
        raise UpstreamError("context_too_long")
    chosen = []
    for pair in reversed(history):
        trial = [pair,*chosen]
        messages = [base[0],*(message for saved in trial for message in saved),base[-1]]
        candidate = chat_body(messages,output_tokens=settings.output_cap)
        if len(messages)>201 or len(json.dumps(candidate,ensure_ascii=False).encode())>500000:
            break
        chosen = trial
    body = minimal
    if chosen:
        while chosen:
            body = chat_body([base[0],*(message for pair in chosen for message in pair),base[-1]],output_tokens=settings.output_cap)
            counted = await provider.count(body)
            budget = min(settings.prompt_cap,counted["context_capacity"]-settings.output_cap-settings.safety_tokens)
            if counted["input_tokens"]<=budget:
                break
            chosen.pop(0)
        if not chosen:
            body = minimal
            counted = await provider.count(body)
    return body,{"history_truncated":len(chosen)<len(history),"prompt_tokens":counted["input_tokens"],"prompt_budget":budget}
