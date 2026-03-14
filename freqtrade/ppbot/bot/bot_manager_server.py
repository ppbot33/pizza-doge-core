"""
Bot Manager Server
==================
独立的 Bot 进程管理 API 服务启动入口。

这是一个独立运行的 FastAPI 服务，与 Freqtrade 原有的 api_server 并列运行，
专门负责管理多个 Freqtrade Bot 进程的生命周期。

启动方式：
    # 直接运行（开发模式，从 pizza-doge-core/ 目录执行）
    python freqtrade/pcsmanage/bot_manager_server.py

    # 指定端口和认证信息
    BOT_MANAGER_PORT=9000 BOT_MANAGER_USERNAME=admin BOT_MANAGER_PASSWORD=secret \
        python freqtrade/pcsmanage/bot_manager_server.py

    # 使用 uvicorn 生产模式（从 pizza-doge-core/ 目录执行）
    uvicorn freqtrade.pcsmanage.bot_manager_server:app --host 0.0.0.0 --port 9000

环境变量配置：
    BOT_MANAGER_HOST         监听地址，默认 0.0.0.0
    BOT_MANAGER_PORT         监听端口，默认 9000
    BOT_MANAGER_USERNAME     API 认证用户名，默认 admin
    BOT_MANAGER_PASSWORD     API 认证密码，默认 pizza_manager（生产环境务必修改）
    BOT_MANAGER_LOG_LEVEL    日志级别，默认 INFO
    BOT_MANAGER_CORS_ORIGINS CORS 允许的来源，逗号分隔，默认允许所有（*）
"""

import logging
import os
import sys
from pathlib import Path


# ─── sys.path 配置 ─────────────────────────────────────────────────────────────
# 本文件位于 pizza-doge-core/freqtrade/ppbot/bot/bot_manager_server.py
# 上溯 4 级到达 pizza-doge-core/，将其加入 sys.path 以确保 freqtrade 包可被正确导入
_PCSMANAGE_DIR = Path(__file__).parent  # pizza-doge-core/freqtrade/ppbot/bot/
_PPBOT_DIR = _PCSMANAGE_DIR.parent  # pizza-doge-core/freqtrade/ppbot
_FREQTRADE_DIR = _PPBOT_DIR.parent  # pizza-doge-core/freqtrade/
_PROJECT_ROOT = _FREQTRADE_DIR.parent  # pizza-doge-core/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from freqtrade.ppbot.bot.api_bot_manager import router as bot_manager_router


# ─── 日志配置 ──────────────────────────────────────────────────────────────────

_LOG_LEVEL = os.environ.get("BOT_MANAGER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── FastAPI 应用创建 ──────────────────────────────────────────────────────────

app = FastAPI(
    title="Pizza Bot Manager API",
    description=(
        "Freqtrade 多策略 Bot 进程管理服务。\n\n"
        "提供对多个 Freqtrade 交易 Bot 进程的独立启动、暂停、恢复、停止管理。\n\n"
        "**认证方式**：HTTP Basic Auth\n\n"
        "**默认凭据**：通过环境变量 `BOT_MANAGER_USERNAME` / `BOT_MANAGER_PASSWORD` 配置。"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── CORS 中间件 ───────────────────────────────────────────────────────────────

_cors_origins_env = os.environ.get("BOT_MANAGER_CORS_ORIGINS", "*")
if _cors_origins_env == "*":
    _cors_origins = ["*"]
else:
    _cors_origins = [origin.strip() for origin in _cors_origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 路由注册 ──────────────────────────────────────────────────────────────────

# Bot 管理路由，所有接口挂载在 /api/v1/bot-manager 下
app.include_router(bot_manager_router, prefix="/api/v1")


# ─── 根路由 ────────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
def root() -> dict:
    """根路径，返回服务基本信息"""
    return {
        "service": "Pizza Bot Manager",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/bot-manager/health",
    }


# ─── 启动入口 ──────────────────────────────────────────────────────────────────


def main() -> None:
    """直接运行此文件时的启动入口"""
    host = os.environ.get("BOT_MANAGER_HOST", "0.0.0.0")
    port = int(os.environ.get("BOT_MANAGER_PORT", "9000"))

    logger.info("=" * 60)
    logger.info("Pizza Bot Manager API 服务启动")
    logger.info(f"  项目根目录: {_PROJECT_ROOT}")
    logger.info(f"  监听地址: http://{host}:{port}")
    logger.info(f"  API 文档: http://{host}:{port}/docs")
    logger.info(f"  健康检查: http://{host}:{port}/api/v1/bot-manager/health")
    logger.info(f"  认证用户: {os.environ.get('BOT_MANAGER_USERNAME', 'admin')}")
    logger.info(f"  CORS 来源: {_cors_origins}")
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=_LOG_LEVEL.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
