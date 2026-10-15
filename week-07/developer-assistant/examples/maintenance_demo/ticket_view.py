"""View state adapter: retain explicit refresh after any resolve conflict."""
from ticket_client import build_resolution, parse_resolution

def resolution_action(ticket, text):
    """Resolve carries the version seen by this view, never an invented version."""
    return build_resolution(ticket["id"], text, ticket["version"])

def resolution_result(response):
    """Resolve conflicts remain visible to the caller; no automatic reopen/retry."""
    return parse_resolution(response)
