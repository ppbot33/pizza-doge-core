"""
Bot Runner - 内存配置启动入口
==============================
通过环境变量 FREQTRADE_CONFIG_JSON 接收 JSON 格式的配置，
直接传入 Worker 启动 Freqtrade Bot，无需配置文件。

使用方式（由 BotProcessManager.start_bot_with_config() 调用）：
    FREQTRADE_CONFIG_JSON='{"exchange": {...}, ...}' python -m freqtrade.pcsmanage.bot_runner
"""

import json
import logging
import os
import sys
from pathlib import Path


# 将 pizza-doge-core 加入 sys.path，确保能正确导入 freqtrade 包
_PCSMANAGE_DIR = Path(__file__).parent  # pizza-doge-core/freqtrade/ppbot/bot/
_PPBOT_DIR = _PCSMANAGE_DIR.parent  # pizza-doge-core/freqtrade/ppbot
_FREQTRADE_DIR = _PPBOT_DIR.parent  # pizza-doge-core/freqtrade/
_PROJECT_ROOT = _FREQTRADE_DIR.parent  # pizza-doge-core/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

ENV_CONFIG_KEY = "FREQTRADE_CONFIG_JSON"


def main() -> None:
    """
    从环境变量读取配置 dict，直接启动 Freqtrade Worker。
    不读取任何配置文件，不修改 Worker 本身的逻辑。
    """
    raw_config = os.environ.get(ENV_CONFIG_KEY)
    if not raw_config:
        print(
            f"[bot_runner] 错误：环境变量 {ENV_CONFIG_KEY} 未设置或为空，无法启动 Bot。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        config: dict = json.loads(raw_config)
    except json.JSONDecodeError as error:
        print(
            f"[bot_runner] 错误：{ENV_CONFIG_KEY} 不是合法的 JSON：{error}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 延迟导入，避免在进程管理服务启动时就加载整个 freqtrade 包
    from freqtrade.enums import RunMode
    from freqtrade.worker import Worker

    # 参考 Configuration._process_runmode 的逻辑：
    # - 如果调用方已经在 config 中显式设置了 runmode，则尊重现有值
    # - 否则根据 dry_run 推导：dry_run=True -> DRY_RUN；dry_run=False -> LIVE
    if "runmode" not in config:
        config["runmode"] = RunMode.DRY_RUN if config.get("dry_run", True) else RunMode.LIVE

    worker = Worker(args={}, config=config)
    worker.run()


if __name__ == "__main__":
    main()
