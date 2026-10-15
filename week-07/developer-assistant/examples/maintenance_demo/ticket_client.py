"""Pure transport adapter. Keep signatures stable for the CLI and view consumers."""

def build_resolution(ticket_id, note, version):
    """Build the request consumed by CLI and view; currently legacy wire names."""
    return {"method": "POST", "path": f"/api/tickets/{ticket_id}/close",
            "json": {"note": note, "version": version}}

def parse_resolution(response):
    """Map response into the public client result; conflicts must stay explicit."""
    if response["status_code"] == 409:
        raise ValueError("Version conflict or already resolved; refresh before retry")
    if response["status_code"] != 200:
        raise ValueError("Ticket request failed")
    body = response["body"]
    return {"id": body["id"], "state": body["state"], "note": body["note"], "version": body["version"]}

def filter_tickets(tickets, status=None, assignee=None):
    """Unrelated behavior: AND filters preserve order and do not mutate inputs."""
    return [ticket for ticket in tickets
            if (status is None or ticket["status"] == status)
            and (assignee is None or ticket["assignee"] == assignee)]
