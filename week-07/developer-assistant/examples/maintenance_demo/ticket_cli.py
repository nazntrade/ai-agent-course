"""CLI preparation adapter; no network or subprocess operations."""
from ticket_client import build_resolution, parse_resolution, filter_tickets

def prepare_resolve(ticket_id, user_note, displayed_version):
    """Resolve uses the client wire adapter and the displayed optimistic version."""
    return build_resolution(ticket_id, user_note, displayed_version)

def present_resolve(response):
    """Resolve result is forwarded without depending on legacy response names."""
    return parse_resolution(response)

def visible_tickets(rows, assignee):
    return filter_tickets(rows, status="open", assignee=assignee)
