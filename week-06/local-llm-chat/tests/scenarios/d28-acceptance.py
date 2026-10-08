"""D28 evidence capture entry point. No heuristic semantic PASS.

Full answers, failed attempts and repeated questions are retained in a timestamped
directory. A separate review of meaning and UI behavior is required.
"""
from pathlib import Path
import sys
MODULE_DIR=Path(__file__).resolve().parent.parent.parent
sys.path.insert(0,str(MODULE_DIR))
from harness.d28_verify import run

def build_preflight_seed(active_model: str) -> dict[str, object]:
    """Build initial preflight artifact seed."""
    return {
        "provider": "local",
        "model": active_model,
        "steps": {},
    }

def build_q_error_artifact(question_text: str, active_model: str, exc: Exception) -> dict:
    """Build q-*.json error artifact with correct model field.

    Called from the question loop exception handler. The regression test
    imports and calls this function directly to verify model correctness.
    """
    error_info = {
        "type": type(exc).__name__,
        "message": str(exc),
        "status_code": getattr(exc, "status_code", None),
        "details": getattr(exc, "details", None),
        "response_body": getattr(exc, "response_body", None),
    }
    return {
        "question": question_text,
        "model": active_model,
        **error_info,
    }

def build_stability_error_artifact(run1_models: list, active_model: str, exc: Exception) -> dict:
    """Build stability.json error artifact with correct model fallback."""
    return {
        "model": run1_models[0] if run1_models else active_model,
        "error": type(exc).__name__,
        "detail": str(exc),
    }

if __name__ == "__main__":
    raise SystemExit(run())
