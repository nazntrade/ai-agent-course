"""Actual SDK stdio failures, without mocked Client or context_call."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from assistant.support_mcp import context_call

ERROR='Support MCP failed; no direct JSON context fallback was used'

class ActualMcpNegative(unittest.IsolatedAsyncioTestCase):
    async def test_missing_snapshot_real_handshake_and_tool_failure_rejects_context(self):
        with tempfile.TemporaryDirectory() as d:
            missing=Path(d)/'missing-scoped-snapshot.json'
            self.assertFalse(missing.exists())
            expected={'snapshot_id':'synthetic-missing','context_version':1,'sha256':'0'*64}
            # The genuine module/server starts and advertises its actual tools.
            # get_user_context then executes against this nonexistent TEMP path.
            with self.assertRaisesRegex(ValueError,'^'+ERROR+'$'):
                await context_call(missing,'demo-alex','synthetic-ticket',expected)
            self.assertFalse(missing.exists(),'Neither MCP nor host may fabricate a missing snapshot')
            print('Actual SDK stdio missing snapshot: tool failure propagated; no context/fallback returned')

    async def test_real_sdk_stdio_startup_failure_returns_no_alternate_context(self):
        with tempfile.TemporaryDirectory() as d:
            executable=Path(d)/'nonexistent-owned-server.exe'
            self.assertFalse(executable.exists())
            expected={'snapshot_id':'synthetic-startup','context_version':1,'sha256':'0'*64}
            # Only the executable path is injected. The shipped context_call,
            # SDK Client and stdio transport all run normally and attempt spawn.
            with patch('assistant.support_mcp.sys.executable',str(executable)):
                with self.assertRaisesRegex(ValueError,'^'+ERROR+'$'):
                    await context_call(Path(d)/'unused.json','demo-alex','synthetic-ticket',expected)
            self.assertFalse(executable.exists())
            self.assertEqual(list(Path(d).iterdir()),[],'No replacement server or alternate context may be created')
            print('Actual SDK stdio spawn failure: unavailable executable propagated; no alternate context returned')
