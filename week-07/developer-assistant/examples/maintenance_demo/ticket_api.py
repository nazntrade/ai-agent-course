"""TicketDesk in-memory API contract. Framework-independent educational service.

The request dispatcher at the end is the authoritative current wire contract.
Storage is intentionally ephemeral. There is no authentication or persistence.
All mutations return defensive copies; sensitive resolution text is not logged.
"""
from copy import deepcopy


class TicketError(ValueError):
    """A transport-neutral error with an explicit status code and error name."""

    def __init__(self, status_code, code):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class TicketDesk:
    """Small ticket service with optimistic concurrency on every mutation."""

    def __init__(self):
        self.tickets = {}
        self.sequence = 0
        self.events = []

    def _find(self, ticket_id):
        """Look up a ticket without exposing the mutable stored dictionary."""
        if ticket_id not in self.tickets:
            raise TicketError(404, "ticket_not_found")
        return self.tickets[ticket_id]

    def _version(self, ticket, expected_version):
        """Booleans are not versions even though bool subclasses int."""
        if type(expected_version) is not int:
            raise TicketError(400, "invalid_expected_version")
        if ticket["version"] != expected_version:
            raise TicketError(409, "version_conflict")

    def _copy(self, ticket):
        """Callers cannot mutate stored labels or metadata through the result."""
        return deepcopy(ticket)

    def create_ticket(self, title, assignee="unassigned", priority="normal"):
        """Create an open ticket at version one, without a resolution."""
        if not isinstance(title, str) or not title.strip():
            raise TicketError(400, "invalid_title")
        if not isinstance(assignee, str) or not assignee.strip():
            raise TicketError(400, "invalid_assignee")
        if priority not in {"low", "normal", "high"}:
            raise TicketError(400, "invalid_priority")
        self.sequence += 1
        ticket_id = "T-" + str(self.sequence)
        ticket = {
            "id": ticket_id,
            "title": title.strip(),
            "assignee": assignee.strip(),
            "priority": priority,
            "status": "open",
            "resolution": None,
            "version": 1,
            "labels": [],
            "metadata": {},
        }
        self.tickets[ticket_id] = ticket
        self.events.append({"id": ticket_id, "action": "created", "version": 1})
        return self._copy(ticket)

    def get_ticket(self, ticket_id):
        """Return one complete defensive ticket snapshot."""
        return self._copy(self._find(ticket_id))

    def list_tickets(self, status=None, assignee=None, priority=None, label=None):
        """Filters compose with AND and preserve ticket creation order."""
        rows = []
        for ticket in self.tickets.values():
            if status is not None and ticket["status"] != status:
                continue
            if assignee is not None and ticket["assignee"] != assignee:
                continue
            if priority is not None and ticket["priority"] != priority:
                continue
            if label is not None and label not in ticket["labels"]:
                continue
            rows.append(self._copy(ticket))
        return rows

    def assign_ticket(self, ticket_id, assignee, expected_version):
        """Assignment keeps status and resolution unchanged."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if not isinstance(assignee, str) or not assignee.strip():
            raise TicketError(400, "invalid_assignee")
        ticket["assignee"] = assignee.strip()
        ticket["version"] += 1
        self.events.append({"id": ticket_id, "action": "assigned", "version": ticket["version"]})
        return self._copy(ticket)

    def set_priority(self, ticket_id, priority, expected_version):
        """Priority changes use the same optimistic concurrency convention."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if priority not in {"low", "normal", "high"}:
            raise TicketError(400, "invalid_priority")
        ticket["priority"] = priority
        ticket["version"] += 1
        self.events.append({"id": ticket_id, "action": "priority_changed", "version": ticket["version"]})
        return self._copy(ticket)

    def rename_ticket(self, ticket_id, title, expected_version):
        """A title edit never reopens a resolved ticket."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if not isinstance(title, str) or not title.strip():
            raise TicketError(400, "invalid_title")
        ticket["title"] = title.strip()
        ticket["version"] += 1
        self.events.append({"id": ticket_id, "action": "renamed", "version": ticket["version"]})
        return self._copy(ticket)

    def add_label(self, ticket_id, label, expected_version):
        """Label insertion is idempotent only at the current version."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if not isinstance(label, str) or not label.strip():
            raise TicketError(400, "invalid_label")
        label = label.strip()
        if label not in ticket["labels"]:
            ticket["labels"].append(label)
            ticket["version"] += 1
            self.events.append({"id": ticket_id, "action": "label_added", "version": ticket["version"]})
        return self._copy(ticket)

    def remove_label(self, ticket_id, label, expected_version):
        """Removing a missing label leaves the current version unchanged."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if label in ticket["labels"]:
            ticket["labels"].remove(label)
            ticket["version"] += 1
            self.events.append({"id": ticket_id, "action": "label_removed", "version": ticket["version"]})
        return self._copy(ticket)

    def set_metadata(self, ticket_id, key, value, expected_version):
        """Only small public metadata strings are supported in this example."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if not isinstance(key, str) or not key or key.startswith("_"):
            raise TicketError(400, "invalid_metadata_key")
        if not isinstance(value, str) or len(value) > 200:
            raise TicketError(400, "invalid_metadata_value")
        ticket["metadata"][key] = value
        ticket["version"] += 1
        self.events.append({"id": ticket_id, "action": "metadata_changed", "version": ticket["version"]})
        return self._copy(ticket)

    def summary(self):
        """Return aggregate counts without including sensitive ticket text."""
        counts = {"open": 0, "resolved": 0}
        for ticket in self.tickets.values():
            counts[ticket["status"]] += 1
        return {"total": len(self.tickets), "by_status": counts}

    def public_events(self, ticket_id=None):
        """Audit records contain IDs/actions/versions, never resolution text."""
        return [dict(event) for event in self.events
                if ticket_id is None or event["id"] == ticket_id]

    def export_metadata(self):
        """A public inventory excludes titles and resolution text."""
        return [{"id": ticket["id"],
                 "status": ticket["status"],
                 "assignee": ticket["assignee"],
                 "priority": ticket["priority"],
                 "version": ticket["version"]}
                for ticket in self.tickets.values()]

    def validate_integrity(self):
        """Check internal invariants without repairing or reopening tickets."""
        for ticket_id, ticket in self.tickets.items():
            if ticket["id"] != ticket_id:
                raise TicketError(500, "identity_invariant")
            if ticket["status"] not in {"open", "resolved"}:
                raise TicketError(500, "status_invariant")
            if ticket["version"] < 1:
                raise TicketError(500, "version_invariant")
            if ticket["status"] == "open" and ticket["resolution"] is not None:
                raise TicketError(500, "resolution_invariant")
            if ticket["status"] == "resolved" and not ticket["resolution"]:
                raise TicketError(500, "resolution_invariant")
        return True

    def assignee_summary(self):
        """Expose counts per assignee without ticket titles or resolution text."""
        counts = {}
        for ticket in self.tickets.values():
            assignee = ticket["assignee"]
            counts[assignee] = counts.get(assignee, 0) + 1
        return counts

    def resolve_ticket(self, ticket_id, resolution, expected_version):
        """Resolve once. Version is checked before the already-resolved policy."""
        ticket = self._find(ticket_id)
        self._version(ticket, expected_version)
        if ticket["status"] == "resolved":
            raise TicketError(409, "already_resolved")
        if not isinstance(resolution, str) or not resolution.strip():
            raise TicketError(400, "invalid_resolution")
        ticket["resolution"] = resolution.strip()
        ticket["status"] = "resolved"
        ticket["version"] += 1
        self.events.append({"id": ticket_id, "action": "resolved", "version": ticket["version"]})
        return {key: ticket[key] for key in ("id", "status", "resolution", "version")}

    def dispatch(self, method, path, body):
        """Current resolve route. Legacy close route has no compatibility alias."""
        parts = path.strip("/").split("/")
        if method != "POST" or len(parts) != 4 or parts[:2] != ["api", "tickets"] or parts[3] != "resolve":
            return {"status_code": 404, "body": {"error": "route_not_found"}}
        if set(body) != {"resolution", "expected_version"}:
            return {"status_code": 400, "body": {"error": "invalid_body"}}
        try:
            value = self.resolve_ticket(parts[2], body["resolution"], body["expected_version"])
            return {"status_code": 200, "body": value}
        except TicketError as error:
            return {"status_code": error.status_code, "body": {"error": error.code}}
