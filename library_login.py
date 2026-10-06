"""One school login and selected database checks using launch.py configuration."""
from __future__ import annotations

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
from paper_search_mcp.comm_download import data_dir
from paper_search_mcp.comm_manifest import persistent_lock


async def main(status_file=None, sources=("cnki", "wos")):
    sources = tuple(sources)
    if not sources or len(set(sources)) != len(sources) or any(source not in {"cnki", "wos"} for source in sources):
        raise ValueError("选择 CNKI、WoS 或两者进行访问检查。")
    checks = {"cnki": comm_cnki.authenticate, "wos": comm_wos.authenticate}
    results, complete, failures, previous = {}, set(), {}, None
    pending_school_source = None
    print("将检查所选数据库访问；如需学校登录，请在统一文献浏览器完成一次登录。无需在此输入账号或密码。", flush=True)
    try:
        while True:
            ordered_sources = ((pending_school_source,) + tuple(source for source in sources if source != pending_school_source)
                               if pending_school_source else sources)
            for source in ordered_sources:
                if source in complete or source in failures:
                    continue
                try:
                    result = await checks[source]()
                except Exception as exc:
                    # Report a useful failure stage without forwarding arbitrary
                    # provider exception text, which can contain session data.
                    result = {"success": False, "status": "error",
                              "message": source + " 访问检查发生异常（" + type(exc).__name__ + "）。"}
                results[source] = result
                if (source == pending_school_source
                        and result.get("authentication_stage") not in {"webvpn_login", "webvpn_portal"}):
                    pending_school_source = None
                if result.get("authenticated"):
                    complete.add(source)
                elif (source == "cnki" and result.get("access_mode") == "direct"
                      and not result.get("authentication_required")):
                    # Preserve the source checkout's existing direct-CNKI CLI.
                    # Completion here does not assert institutional authentication.
                    complete.add(source)
                elif result.get("authentication_stage") in {"webvpn_login", "webvpn_portal"}:
                    # Only one provider polls a pending shared school login.
                    pending_school_source = source
                    complete.clear()
                    for other in sources:
                        if other != source and other not in failures:
                            results[other] = {"status": "waiting_for_shared_school_login"}
                    break
                elif not (result.get("authentication_required") or result.get("status") == "needs_attention"):
                    failures[source] = result.get("message", source + " 访问检查失败")
                    # A database failure must not hide the other database's state.
            state = {"shared_school_login": not all(result.get("access_mode") == "direct" for result in results.values()),
                     "authenticated": all(results.get(source, {}).get("authenticated", False) for source in sources),
                     "completed": len(complete) == len(sources), "resources": results}
            safe = json.dumps(state, ensure_ascii=False)
            if safe != previous:
                print(safe, flush=True)
                if status_file:
                    path = Path(status_file)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(safe, encoding="utf-8")
                previous = safe
            if failures:
                raise RuntimeError("；".join(failures[source] for source in sources if source in failures))
            if state["completed"]:
                if any(result.get("access_mode") == "direct" for result in results.values()):
                    print("所选访问检查已完成；CNKI 当前为直连模式，校外请配置 COMM_CNKI_ACCESS_MODE=bfsu_webvpn。", flush=True)
                else:
                    saved = all(results[source].get("session_persistence", {}).get("gateway_save") == "saved"
                                for source in sources)
                    print(("学校登录和所选数据库访问检查完成，会话已加密保存。下次启动可尝试复用。"
                           if saved else "访问检查已完成，但部分会话未确认保存；请查看 session_persistence 诊断。"), flush=True)
                return state
            await asyncio.sleep(3)
    finally:
        worker = getattr(comm_cnki, "_worker", None)
        if worker:
            await asyncio.to_thread(worker.close)


def run(argv=None):
    parser = argparse.ArgumentParser(description="统一学校登录，并分别检查 CNKI / WoS 访问。")
    parser.add_argument("provider", nargs="?", choices=("cnki", "wos"), help="兼容旧的单数据库位置参数")
    parser.add_argument("--source", choices=("both", "cnki", "wos"), help="默认检查两者")
    parser.add_argument("--status-file", type=Path)
    args = parser.parse_args(argv)
    if args.provider and args.source and args.provider != args.source:
        parser.error("位置参数与 --source 指定了不同数据库。")
    selected = args.source or args.provider or "both"
    sources = ("cnki", "wos") if selected == "both" else (selected,)
    try:
        # Hold one helper lease through worker/browser shutdown. The lock file
        # remains on disk; the OS lock is released when this scope ends.
        with persistent_lock(data_dir() / "cnki" / "institution-login.lock"):
            asyncio.run(main(args.status_file, sources))
    except KeyboardInterrupt:
        print("已停止等待；文件和登录缓存均保留。", flush=True)
        return 130
    except Exception as exc:
        if str(exc) in {"workflow_busy: another operation is in progress", "workflow_lock_error"}:
            print("统一登录入口已被占用或无法取得锁，请查看现有文献浏览器；本次未启动另一个登录进程。",
                  file=sys.stderr, flush=True)
        else:
            print(str(exc) or "登录访问检查未完成。", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
