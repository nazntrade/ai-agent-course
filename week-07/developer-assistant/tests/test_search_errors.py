import unittest
import tempfile
from pathlib import Path
from assistant.store import Store,ident,now
from assistant.tools import validate_search_result

class SearchErrors(unittest.TestCase):
    def test_embedded_error_is_failure(self):
        with self.assertRaises(ValueError):
            validate_search_result({'error':'Search failed','status':429})
    def test_status_failure_is_not_success(self):
        with self.assertRaises(ValueError):
            validate_search_result({'status':500,'results':[]})
    def test_results_must_be_attributed(self):
        with self.assertRaises(ValueError):
            validate_search_result({'results':[{'content':'not attributed'}]})
        validate_search_result({'results':[{'url':'https://example.org','content':'public'}]})
        validate_search_result({'results':[]})
    def test_historical_false_completion_corrected_without_erasing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db';store=Store(path)
            error={'error':'Search failed','status':429}
            import json
            t=dict(id=ident(),project_id='fixture',created=now(),mode='web',status='completed',answer=json.dumps(error),events=[{'result':error}],plan=[{'status':'completed'}])
            store.save_task(t);recovered=Store(path).task(t['id'])
            self.assertEqual(recovered['status'],'failed');self.assertEqual(recovered['answer'],'')
            self.assertEqual(recovered['events'][0]['result'],error)
