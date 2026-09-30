"""Source checkout launcher for Find_comm_papers; keep personal data outside Git."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
if (ROOT / ".env").is_file():
    os.environ.setdefault("PAPER_SEARCH_MCP_ENV_FILE", str(ROOT / ".env"))
from paper_search_mcp.config import load_env_file
load_env_file()
os.environ.setdefault("COMM_MCP_PROFILE", str(ROOT / "paper_search_mcp" / "comm_profile.json"))
os.environ.setdefault("COMM_MCP_DATA_DIR", str(Path.home() / "Documents" / "FindPapersData"))

from paper_search_mcp import comm_server

if "--self-check" in sys.argv:
    tools = asyncio.run(comm_server.mcp.list_tools())
    print(json.dumps({"name":"Find_comm_papers", "version":"0.5.0",
                      "tool_count":len(tools), "tools":sorted(t.name for t in tools)}, ensure_ascii=False))
else:
    comm_server.main()
