"""
Bot Manager API Router
======================
基于 FastAPI 的 Bot 进程管理 API 路由。
提供对多个策略 Bot 进程的独立启动、暂停、恢复、停止管理接口。

认证由 Freqtrade webserver 统一处理（http_basic_or_jwt_token），无需在此重复认证。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from freqtrade.ppbot.bot.bot_process_manager import (
    BOT_STATUS_ERROR,
    BOT_STATUS_PAUSED,
    BOT_STATUS_RUNNING,
    BOT_STATUS_STOPPED,
    BotProcessInfo,
    get_manager,
)
from freqtrade.rpc.api_server.api_accounts import load_accounts
from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)

# ─── 请求/响应模型 ─────────────────────────────────────────────────────────────


class StartBotRequest(BaseModel):
    """启动 Bot 的请求体"""

    strategy_name: str = Field(
        ...,
        description="策略类名，如 'MyStrategy'",
        examples=["MyStrategy"],
    )
    config_path: str = Field(
        ...,
        description="配置文件路径（相对于 pizza-doge-core/ 目录或绝对路径）",
        examples=["user_data/configs/strategy_a.json"],
    )


class StartBotWithConfigRequest(BaseModel):
    """通过内存参数启动 Bot 的请求体（无需配置文件）"""

    strategy_name: str = Field(
        ...,
        description="策略类名，如 'MyStrategy'",
        examples=["MyStrategy"],
    )
    account_id: int = Field(
        ...,
        description="账户 ID，服务端将根据此 ID 从 accounts.json 查询交易所配置",
        examples=[1],
    )
    dry_run: bool = Field(
        default=True,
        description="是否为模拟盘（true=模拟盘，false=实盘）",
    )
    stake_amount: float = Field(
        ...,
        description="每笔交易投入金额",
        examples=[100.0],
        gt=0,
    )
    stake_currency: str = Field(
        default="USDT",
        description="计价货币，如 'USDT'、'BTC'、'ETH'",
        examples=["USDT"],
    )
    api_port: int = Field(
        ...,
        description="Bot 的 api_server 监听端口，同一机器上多个 Bot 必须使用不同端口",
        examples=[8080],
        gt=1024,
        lt=65536,
    )
    max_open_trades: int = Field(
        default=3,
        description="最大同时持仓数量，-1 表示不限制",
        ge=-1,
    )
    timeframe: str = Field(
        default="5m",
        description="K 线时间周期，如 '1m'、'5m'、'15m'、'1h'",
        examples=["5m"],
    )
    api_username: str = Field(
        default="freqtrader",
        description="Bot 自身 API Server 的用户名（用于暂停/恢复操作）",
    )
    api_password: str = Field(
        default="pizza_bot",
        description="Bot 自身 API Server 的密码",
    )


class StopBotRequest(BaseModel):
    """停止 Bot 的请求体"""

    force: bool = Field(
        default=False,
        description="是否强制终止（跳过优雅退出，直接 SIGKILL）",
    )


class BotStatusResponse(BaseModel):
    """Bot 状态响应"""

    strategy_name: str
    status: str = Field(
        description=f"状态：{BOT_STATUS_RUNNING}/{BOT_STATUS_PAUSED}/{BOT_STATUS_STOPPED}/{BOT_STATUS_ERROR}"
    )
    pid: Optional[int] = None
    config_path: str
    api_port: Optional[int] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    log_file: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def from_process_info(cls, info: BotProcessInfo) -> "BotStatusResponse":
        return cls(
            strategy_name=info.strategy_name,
            status=info.status,
            pid=info.pid,
            config_path=info.config_path,
            api_port=info.api_port,
            started_at=info.started_at,
            stopped_at=info.stopped_at,
            log_file=info.log_file,
            error_message=info.error_message,
        )


class BotListResponse(BaseModel):
    """Bot 列表响应"""

    total: int
    bots: list[BotStatusResponse]


class OperationResponse(BaseModel):
    """操作结果响应"""

    success: bool
    message: str
    bot: Optional[BotStatusResponse] = None


# ─── 路由定义 ──────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/bot-manager",
    tags=["Bot Manager"],
)


@router.get(
    "/bots",
    response_model=BotListResponse,
    summary="列出所有 Bot",
    description="返回所有已注册的 Bot 进程信息，包括运行中和已停止的历史记录。",
)
def list_bots() -> BotListResponse:
    manager = get_manager()
    all_bots = manager.list_all_bots()
    return BotListResponse(
        total=len(all_bots),
        bots=[BotStatusResponse.from_process_info(bot) for bot in all_bots],
    )


@router.get(
    "/bots/{strategy_name}",
    response_model=BotStatusResponse,
    summary="查询指定策略的 Bot 状态",
    description="返回指定策略名称的 Bot 进程详细状态。",
)
def get_bot_status(
    strategy_name: str,
) -> BotStatusResponse:
    manager = get_manager()
    info = manager.get_bot_status(strategy_name)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略 [{strategy_name}] 没有注册记录",
        )
    return BotStatusResponse.from_process_info(info)


@router.post(
    "/bots/start",
    response_model=OperationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="启动 Bot",
    description=(
        "为指定策略启动一个独立的 Freqtrade Bot 进程。\n\n"
        "**约束**：同一策略同时只能运行一个进程，重复启动会返回 409 错误。\n\n"
        "**配置文件**：每个策略需要独立的配置文件，配置中的 `db_url` 和 "
        "`api_server.listen_port` 必须与其他策略不同，避免冲突。"
    ),
)
def start_bot(
    request: StartBotRequest,
) -> OperationResponse:
    manager = get_manager()
    try:
        info = manager.start_bot(
            strategy_name=request.strategy_name,
            config_path=request.config_path,
        )
        return OperationResponse(
            success=True,
            message=f"Bot [{request.strategy_name}] 启动成功，PID={info.pid}",
            bot=BotStatusResponse.from_process_info(info),
        )
    except ValueError as error:
        # 策略已在运行
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.post(
    "/bots/start-with-config",
    response_model=OperationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="通过内存参数启动 Bot（无需配置文件）",
    description=(
        "前端传入交易所信息、模拟盘开关、每笔交易金额等核心参数，"
        "服务端在内存中构建完整的 config dict，通过环境变量传入子进程启动 Bot。\n\n"
        "**优势**：API Key 等敏感信息不落盘，每个子进程的环境变量完全隔离。\n\n"
        "**约束**：同一策略同时只能运行一个进程；`api_port` 在同一机器上必须唯一。"
    ),
)
def start_bot_with_config(
    request: StartBotWithConfigRequest,
    ft_config: dict = Depends(get_config),
) -> OperationResponse:
    manager = get_manager()

    # 根据 account_id 从 accounts.json 查询账户信息
    accounts = load_accounts(ft_config)
    account = next((acc for acc in accounts if acc.get("id") == request.account_id), None)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账户 ID [{request.account_id}] 不存在，请先在账户管理页面添加账户",
        )

    exchange_name = account.get("exchange", "")
    exchange_key = account.get("apiKey", "")
    exchange_secret = account.get("apiSecret", "")
    exchange_password = account.get("apiPassword", "")

    if not exchange_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"账户 [{account.get('name')}] 未配置交易所名称",
        )

    # 在内存中构建完整的 Freqtrade config dict
    config = _build_config_from_request(
        request=request,
        exchange_name=exchange_name,
        exchange_key=exchange_key,
        exchange_secret=exchange_secret,
        exchange_password=exchange_password,
    )

    try:
        info = manager.start_bot_with_config(
            strategy_name=request.strategy_name,
            config=config,
            api_port=request.api_port,
        )
        return OperationResponse(
            success=True,
            message=f"Bot [{request.strategy_name}] 启动成功（内存配置），PID={info.pid}",
            bot=BotStatusResponse.from_process_info(info),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
        ) from error


def _build_config_from_request(
    request: StartBotWithConfigRequest,
    exchange_name: str,
    exchange_key: str,
    exchange_secret: str,
    exchange_password: str,
) -> dict:
    """
    根据前端传入的参数和账户查询到的交易所信息，在内存中构建完整的 Freqtrade config dict。
    所有必要字段均在此处设置默认值，确保 Worker 能正常启动。
    """
    strategy_name = request.strategy_name
    dry_run = request.dry_run

    # 数据库文件按策略名隔离，避免多个 Bot 共用同一个 sqlite 文件
    db_filename = f"{strategy_name}.dryrun.sqlite" if dry_run else f"{strategy_name}.sqlite"
    db_url = f"sqlite:///user_data/tradebot/{db_filename}"

    return {
        # ── 基础运行配置 ──────────────────────────────────────────────
        "strategy": strategy_name,
        "dry_run": dry_run,
        "timeframe": request.timeframe,
        "max_open_trades": request.max_open_trades,
        # ── 资金配置 ──────────────────────────────────────────────────
        "stake_currency": request.stake_currency,
        "stake_amount": request.stake_amount,
        "tradable_balance_ratio": 0.99,
        "amend_last_stake_amount": False,
        # ── 交易所配置（从账户信息中获取，不落盘）────────────────────
        "exchange": {
            "name": exchange_name,
            "key": exchange_key,
            "secret": exchange_secret,
            "password": exchange_password,
            "sandbox": False,
            "ccxt_config": {},
            "ccxt_async_config": {},
        },
        # ── 数据库 ────────────────────────────────────────────────────
        "db_url": db_url,
        # ── Bot 自身的 API Server 配置 ────────────────────────────────
        # 用于进程管理服务后续调用 pause/resume 接口
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": request.api_port,
            "verbosity": "error",
            "enable_openapi": False,
            "jwt_secret_key": f"pizza_bot_{strategy_name}_{request.api_port}",
            "CORS_origins": [],
            "username": request.api_username,
            "password": request.api_password,
        },
        # ── 数据目录 ──────────────────────────────────────────────────
        "datadir": "user_data/data",
        "user_data_dir": "user_data",
        # ── 日志 ──────────────────────────────────────────────────────
        "verbosity": 0,
        # ── 其他必要默认值 ────────────────────────────────────────────
        "initial_state": "running",
        "force_entry_enable": False,
        "internals": {
            "process_throttle_secs": 5,
        },
    }


@router.post(
    "/bots/{strategy_name}/pause",
    response_model=OperationResponse,
    summary="暂停 Bot（停止开新仓）",
    description=(
        "暂停指定策略的 Bot，停止开新仓，但继续管理已有持仓（平仓、止损等）。\n\n"
        "通过调用 Bot 自身的 `/api/v1/pause` 接口实现，需要 Bot 配置中启用 `api_server`。"
    ),
)
def pause_bot(
    strategy_name: str,
) -> OperationResponse:
    manager = get_manager()
    try:
        info = manager.pause_bot(strategy_name)
        return OperationResponse(
            success=True,
            message=f"Bot [{strategy_name}] 已暂停（停止开新仓，继续管理已有仓位）",
            bot=BotStatusResponse.from_process_info(info),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.post(
    "/bots/{strategy_name}/resume",
    response_model=OperationResponse,
    summary="恢复 Bot（重新允许开仓）",
    description=(
        "恢复已暂停的 Bot，重新允许开新仓。\n\n通过调用 Bot 自身的 `/api/v1/start` 接口实现。"
    ),
)
def resume_bot(
    strategy_name: str,
) -> OperationResponse:
    manager = get_manager()
    try:
        info = manager.resume_bot(strategy_name)
        return OperationResponse(
            success=True,
            message=f"Bot [{strategy_name}] 已恢复运行",
            bot=BotStatusResponse.from_process_info(info),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.post(
    "/bots/{strategy_name}/stop",
    response_model=OperationResponse,
    summary="停止 Bot",
    description=(
        "停止指定策略的 Bot 进程。\n\n"
        "默认优雅退出（先调用 Bot API 停止，再发送 SIGTERM，等待 15 秒后若未退出则 SIGKILL）。\n\n"
        "设置 `force=true` 可跳过优雅退出直接强制终止。"
    ),
)
def stop_bot(
    strategy_name: str,
    request: StopBotRequest = StopBotRequest(),
) -> OperationResponse:
    manager = get_manager()
    try:
        info = manager.stop_bot(strategy_name, force=request.force)
        return OperationResponse(
            success=True,
            message=f"Bot [{strategy_name}] 已停止",
            bot=BotStatusResponse.from_process_info(info),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.delete(
    "/bots/{strategy_name}",
    response_model=OperationResponse,
    summary="删除 Bot 记录",
    description=(
        "从注册表中删除已停止的 Bot 历史记录。\n\n"
        "**注意**：只能删除状态为 `stopped` 或 `error` 的记录，运行中的 Bot 需先停止。"
    ),
)
def delete_bot_record(
    strategy_name: str,
) -> OperationResponse:
    manager = get_manager()
    try:
        manager.remove_bot_record(strategy_name)
        return OperationResponse(
            success=True,
            message=f"Bot [{strategy_name}] 记录已删除",
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.get(
    "/health",
    summary="健康检查",
    description="检查 Bot Manager API 服务是否正常运行（无需认证）。",
)
def health_check() -> dict:
    return {"status": "ok", "service": "bot-manager"}
