"""Project-readable contract examples; owner checker is stored outside this tree."""
import unittest
from ticket_api import TicketDesk

class ResolveContract(unittest.TestCase):
    def setUp(self):
        self.api=TicketDesk()
        self.ticket=self.api.create_ticket("Example", "alex")
    def resolve(self, resolution=" done ", expected_version=1):
        return self.api.dispatch("POST", "/api/tickets/T-1/resolve", {"resolution":resolution,"expected_version":expected_version})
    def test_resolve_success(self):
        r=self.resolve()
        self.assertEqual(r["body"],{"id":"T-1","status":"resolved","resolution":"done","version":2})
    def test_resolve_trims_text(self):
        self.assertEqual(self.resolve()["body"]["resolution"],"done")
    def test_resolve_blank(self):
        self.assertEqual(self.resolve(" ")["body"]["error"],"invalid_resolution")
    def test_resolve_missing_version(self):
        self.assertEqual(self.resolve(expected_version=None)["status_code"],400)
    def test_resolve_boolean_version(self):
        self.assertEqual(self.resolve(expected_version=True)["status_code"],400)
    def test_resolve_stale_version(self):
        self.assertEqual(self.resolve(expected_version=0)["body"]["error"],"version_conflict")
    def test_resolve_once(self):
        self.resolve()
        self.assertEqual(self.resolve(expected_version=2)["body"]["error"],"already_resolved")
    def test_resolve_stale_before_already(self):
        self.resolve()
        self.assertEqual(self.resolve()["body"]["error"],"version_conflict")
    def test_resolve_no_sensitive_audit(self):
        self.resolve("sensitive synthetic text")
        self.assertNotIn("sensitive synthetic text",str(self.api.public_events()))
    def test_resolve_no_reopen(self):
        self.resolve()
        self.api.rename_ticket("T-1","Renamed",2)
        self.assertEqual(self.api.get_ticket("T-1")["status"],"resolved")
    def test_resolve_legacy_route_absent(self):
        self.assertEqual(self.api.dispatch("POST","/api/tickets/T-1/close",{"note":"done","version":1})["status_code"],404)
    def test_resolve_old_fields_rejected(self):
        self.assertEqual(self.api.dispatch("POST","/api/tickets/T-1/resolve",{"note":"done","version":1})["status_code"],400)
    def test_unrelated_filters(self):
        self.api.create_ticket("Another","sam")
        self.assertEqual([r["id"] for r in self.api.list_tickets(status="open",assignee="alex")],["T-1"])
