"""Restricted MCP notification server; accepts only saved report IDs."""
import argparse
import os
from pathlib import Path
from mcp.server import MCPServer
from .config import Settings
from .store import Store
from .tools import deliver_saved_report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);args=parser.parse_args()
    settings=Settings(data=Path(args.data),telegram_token=os.getenv('TELEGRAM_BOT_TOKEN',''),telegram_chat=os.getenv('TELEGRAM_CHAT_ID',''))
    store=Store(settings.data/'assistant.sqlite3',recover=False)
    server=MCPServer('Developer Assistant Owner Reports',version='1.0.0')
    @server.tool()
    async def send_self_report(report_id: str) -> dict:
        """Send a saved completed report to the fixed owner; never accept recipient/text."""
        if len(report_id)!=32 or not all(c in '0123456789abcdef' for c in report_id):
            raise ValueError('Invalid saved report identifier')
        return await deliver_saved_report(settings,store,report_id)
    server.run(transport='stdio')

if __name__=='__main__':
    main()
