"""One bounded model correction for mechanically invalid citations.

Neither source existence nor this validation establishes semantic correctness.
Original attempts remain in the result for audit; no quotation is silently edited.
"""
import json
from ..providers.base import ChatMessage
from .citations import CitationVerifier

def generate_grounded(provider, messages, fragments, options=None):
    result=provider.chat(messages, options=options) if options else provider.chat(messages)
    if not fragments:
        return result
    verifier=CitationVerifier(fragments)
    report=verifier.verify(result.text)
    errors=[c.to_dict() for c in report.citations if c.status in ("unknown_reference","quote_mismatch")]
    attempts=[{"text":result.text,"model":result.model,"finish_reason":result.finish_reason,"latency_ms":result.latency_ms,"usage":result.usage.to_dict() if result.usage else None,"citation_check":report.to_dict()}]
    if errors:
        correction=("Your answer failed exact citation validation. Correct it once using the SAME supplied fragments. "
            "Use only references [1] through ["+str(len(fragments))+"]. Bibliography numbers inside the PDF are not fragment references. "
            "Do not join disjoint words into a quotation. Copy very short contiguous phrases exactly, including extraction artifacts, "
            "or paraphrase WITHOUT quotation marks and cite the supporting fragment. Preserve supported facts; explicitly state missing evidence. "
            "Provide the corrected answer, not an explanation of these instructions. Validation errors: "+json.dumps(errors))
        previous=result
        corrected_messages=[*messages,ChatMessage("assistant",previous.text),ChatMessage("user",correction)]
        result=provider.chat(corrected_messages, options=options) if options else provider.chat(corrected_messages)
        checked=verifier.verify(result.text)
        attempts.append({"text":result.text,"model":result.model,"finish_reason":result.finish_reason,"latency_ms":result.latency_ms,"usage":result.usage.to_dict() if result.usage else None,"citation_check":checked.to_dict()})
        result.latency_ms=round(previous.latency_ms+result.latency_ms,3)
    result.parameters={**result.parameters,"grounding_attempts":attempts,"semantic_quality":"NOT_ASSESSED"}
    return result
