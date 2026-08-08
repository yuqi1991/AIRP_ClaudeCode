"""Disposable AIRP server used by the Playwright release-acceptance matrix."""

from __future__ import annotations

import signal
import tempfile
import threading
from pathlib import Path

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "airp" / "web"


def main() -> None:
    stopped = threading.Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with tempfile.TemporaryDirectory(prefix="airp-browser-") as folder:
        root = Path(folder)
        card = root / "card"
        (card / "memory").mkdir(parents=True)
        (card / ".initvar.json").write_text("{}", encoding="utf-8")
        (card / "chat_log.json").write_text("[]", encoding="utf-8")
        (card / ".card_data.json").write_text(
            '{"name":"AIRP 验收游戏","description":"用于浏览器发布验收。"}',
            encoding="utf-8",
        )
        runtime = SessionTurnRuntime(
            database_path=root / "runtime.sqlite3",
            card_folder=card,
            projection_root=root / "projection",
            bootstrap_legacy_history=False,
        )
        with SessionRuntimeServer(
            runtime,
            static_root=WEB_ROOT,
            workspace=root / "workspace",
        ) as server:
            print(server.base_url, flush=True)
            stopped.wait()


if __name__ == "__main__":
    main()
