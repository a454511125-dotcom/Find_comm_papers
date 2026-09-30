"""Interactive school authentication using the same configuration as launch.py."""
import argparse
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
os.environ.setdefault("COMM_MCP_DATA_DIR", str(Path.home() / "Documents" / "FindPapersData"))
from paper_search_mcp import comm_cnki, comm_wos


async def authenticate(provider):
    previous = None
    print("请在文献浏览器中完成学校登录；无需在终端输入账号或密码。", flush=True)
    try:
        while True:
            result = await provider.authenticate()
            safe = json.dumps(result, ensure_ascii=False)
            if safe != previous:
                print(safe, flush=True)
                previous = safe
            if result.get("authenticated"):
                print("机构身份已确认。WebVPN 网关会话已加密保存。", flush=True)
                return 0
            if result.get("access_mode") == "direct" and not result.get("authentication_required"):
                print("当前为 CNKI 直连模式；校外请配置 COMM_CNKI_ACCESS_MODE=bfsu_webvpn。", flush=True)
                return 0
            if not result.get("authentication_required"):
                print(result.get("message", "认证未完成"), file=sys.stderr, flush=True)
                return 1
            await asyncio.sleep(3)
    finally:
        if comm_cnki._worker:
            await asyncio.to_thread(comm_cnki._worker.close)


def main():
    parser = argparse.ArgumentParser(description="CNKI / WoS 学校网页登录")
    parser.add_argument("provider", choices=("cnki", "wos"))
    args = parser.parse_args()
    try:
        return asyncio.run(authenticate(comm_cnki if args.provider == "cnki" else comm_wos))
    except KeyboardInterrupt:
        print("已停止等待；文件和登录缓存均保留。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
