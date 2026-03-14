"""
Bot Manager API Router
======================
基于 FastAPI 的 Bot 进程管理 API 路由。
提供对多个策略 Bot 进程的独立启动、暂停、恢复、停止管理接口。

认证由 Freqtrade webserver 统一处理（http_basic_or_jwt_token），无需在此重复认证。
"""

import json
import logging
import secrets
import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from datetime import datetime

from freqtrade.ppbot.bot.bot_process_manager import (
    BOT_STATUS_ERROR,
    BOT_STATUS_PAUSED,
    BOT_STATUS_RUNNING,
    BOT_STATUS_STOPPED,
    BotProcessInfo,
    get_manager,
)
from freqtrade.ppbot.storage.accounts_storage import load_accounts
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
    timeframe: Optional[str] = Field(
        default=None,
        description="K 线时间周期，如 '1m'、'5m'、'15m'、'1h'。不传则使用策略类中定义的 timeframe（官方行为）",
        examples=["15m"],
    )
    api_username: str = Field(
        default="pizza-bot",
        description="Bot 自身 API Server 的用户名（用于暂停/恢复操作）",
    )
    api_password: str = Field(
        default="pizza_bot_A.*",
        description="Bot 自身 API Server 的密码",
    )
    bot_name: str = Field(
        default="ppbot",
        description="交易 Bot 名称，对应 config 中的 bot_name",
    )
    advanced_config: Optional[str] = Field(
        default=None,
        description="高级配置 JSON 字符串，与官方 config 结构一致，会与基础配置深合并（覆盖同名字段）",
    )


class StopBotRequest(BaseModel):
    """停止 Bot 的请求体"""

    force: bool = Field(
        default=False,
        description="是否强制终止（跳过优雅退出，直接 SIGKILL）",
    )


def _read_bot_config_dry_run_and_trading_mode(config_path: str | None) -> tuple[Optional[bool], Optional[str]]:
    """从配置文件读取 dry_run 和 trading_mode，失败返回 (None, None)。"""
    import json
    if not config_path:
        return None, None
    try:
        with open(str(config_path), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        dry_run = cfg.get("dry_run")
        if dry_run is not None:
            dry_run = bool(dry_run)
        trading_mode = cfg.get("trading_mode")
        if isinstance(trading_mode, str) and trading_mode:
            return dry_run, trading_mode
        return dry_run, None
    except Exception:
        return None, None


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
    bot_name: Optional[str] = Field(None, description="Bot 名称（展示用）")
    account_id: Optional[int] = Field(None, description="关联的交易账户 ID")
    account_name: Optional[str] = Field(None, description="关联的交易账户名称")
    dry_run: Optional[bool] = Field(None, description="是否模拟盘（从 config 读取）")
    trading_mode: Optional[str] = Field(None, description="现货 spot / 期货 futures（从 config 读取）")
    position_pairs: Optional[list[str]] = Field(None, description="当前持仓交易对（运行中时从 status 拉取）")

    @classmethod
    def from_process_info(
        cls,
        info: BotProcessInfo,
        account_name: Optional[str] = None,
        dry_run: Optional[bool] = None,
        trading_mode: Optional[str] = None,
        position_pairs: Optional[list[str]] = None,
    ) -> "BotStatusResponse":
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
            bot_name=getattr(info, "bot_name", None),
            account_id=getattr(info, "account_id", None),
            account_name=account_name,
            dry_run=dry_run,
            trading_mode=trading_mode,
            position_pairs=position_pairs,
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
    "/aggregate/profit",
    summary="汇总各 Bot 盈亏",
    description=(
        "向所有运行中的 Bot 请求 /api/v1/profit 并合并为一份结果，"
        "与官方 /profit 结构兼容，供前端轮询展示总盈亏。"
    ),
)
def aggregate_profit() -> dict:
    manager = get_manager()
    return manager.aggregate_profit()


@router.get(
    "/health",
    summary="健康检查",
    description="检查 Bot Manager API 服务是否正常运行（无需认证）。",
)
def health_check() -> dict:
    return {"status": "ok", "service": "bot-manager"}


@router.get(
    "/bots",
    response_model=BotListResponse,
    summary="列出所有 Bot",
    description="返回所有已注册的 Bot 进程信息，包括运行中和已停止的历史记录。",
)
def list_bots(ft_config: dict = Depends(get_config)) -> BotListResponse:
    manager = get_manager()
    all_bots = manager.list_all_bots()
    accounts = load_accounts(ft_config)
    id_to_name = {a.get("id"): a.get("name") for a in accounts if a.get("id") is not None}
    bots = []
    for bot in all_bots:
        dry_run, trading_mode = _read_bot_config_dry_run_and_trading_mode(bot.config_path)
        position_pairs = None
        if bot.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
            try:
                trades = manager.get_bot_open_trades(bot.strategy_name)
                position_pairs = [t.get("pair") for t in trades if isinstance(t, dict) and t.get("pair")]
            except Exception:
                pass
        bots.append(
            BotStatusResponse.from_process_info(
                bot,
                account_name=id_to_name.get(getattr(bot, "account_id", None)) if getattr(bot, "account_id", None) else None,
                dry_run=dry_run,
                trading_mode=trading_mode,
                position_pairs=position_pairs or None,
            )
        )
    return BotListResponse(total=len(bots), bots=bots)


# 注意：所有带子路径的 /bots/{strategy_name}/xxx 必须放在 /bots/{strategy_name} 之前，
# 否则 FastAPI 可能把 "Bandtastic/status" 等整体匹配到 strategy_name，导致 404。
@router.get(
    "/bots/{strategy_name}/status",
    summary="Bot 持仓状态（代理）",
    description="请求指定 Bot 的 /api/v1/status，返回当前持仓列表。Bot 未运行时返回空列表。",
)
def get_bot_status_proxy(strategy_name: str):
    """代理到 Bot 的 /api/v1/status，返回 open trades 列表。"""
    manager = get_manager()
    try:
        trades = manager.get_bot_open_trades(strategy_name)
        return trades
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/locks",
    summary="Bot 锁对列表（代理）",
    description="请求指定 Bot 的 /api/v1/locks，返回当前锁对（locks）。Bot 未运行时返回空锁对。",
)
def get_bot_locks_proxy(strategy_name: str):
    """代理到 Bot 的 /api/v1/locks。"""
    manager = get_manager()
    try:
        locks = manager.get_bot_locks(strategy_name)
        return locks
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/profit",
    summary="Bot 盈亏（代理）",
    description="请求指定 Bot 的 /api/v1/profit，返回盈亏汇总。Bot 未运行时返回零值。",
)
def get_bot_profit_proxy(strategy_name: str):
    """代理到 Bot 的 /api/v1/profit。"""
    manager = get_manager()
    try:
        return manager.get_bot_profit(strategy_name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


class PairCandlesRequestBody(BaseModel):
    """与官方 POST /api/v1/pair_candles 请求体一致，columns 为字符串数组"""

    pair: str
    timeframe: str
    limit: Optional[int] = None
    columns: Optional[list[str]] = None


@router.post(
    "/bots/{strategy_name}/pair_candles",
    summary="Bot K 线（POST，支持 columns 字符串数组）",
    description="与官方一致：POST 请求体含 pair、timeframe、limit、columns（字符串数组），返回含 Entry/Exit 标记列。仅 Bot 运行中时可调用。",
)
def post_bot_pair_candles_proxy(strategy_name: str, body: PairCandlesRequestBody):
    """代理到 Bot 的 POST /api/v1/pair_candles，请求体原样转发。"""
    manager = get_manager()
    try:
        return manager.get_bot_pair_candles(
            strategy_name,
            pair=body.pair,
            timeframe=body.timeframe,
            limit=body.limit,
            columns=body.columns,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/pair_candles",
    summary="Bot K 线（GET，无 columns）",
    description="请求指定 Bot 的 GET /api/v1/pair_candles，返回基础 K 线。需 Entry/Exit 标记时请用 POST 并传 columns。仅 Bot 运行中时可调用。",
)
def get_bot_pair_candles_proxy(
    strategy_name: str,
    pair: str = Query(..., description="交易对，如 BTC/USDT"),
    timeframe: str = Query(..., description="时间周期，如 5m、1h"),
    limit: Optional[int] = Query(None, description="K 线数量"),
):
    """代理到 Bot 的 GET /api/v1/pair_candles（不传 columns）。"""
    manager = get_manager()
    try:
        return manager.get_bot_pair_candles(
            strategy_name, pair=pair, timeframe=timeframe, limit=limit, columns=None
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/whitelist",
    summary="Bot 交易对白名单（代理）",
    description="请求指定 Bot 的 /api/v1/whitelist，返回当前交易对列表。Bot 未运行时返回空列表。",
)
def get_bot_whitelist_proxy(strategy_name: str):
    """代理到 Bot 的 /api/v1/whitelist。"""
    manager = get_manager()
    try:
        return manager.get_bot_whitelist(strategy_name)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/balance",
    summary="Bot 账户资产（代理）",
    description="请求指定 Bot 的 /api/v1/balance，返回 currencies、note 等（与官方 Balances 一致）。Bot 未运行时返回空 currencies。",
)
def get_bot_balance_proxy(strategy_name: str):
    manager = get_manager()
    try:
        return manager.get_bot_balance(strategy_name)
    except ValueError:
        return {"currencies": [], "total": 0, "total_bot": 0, "symbol": "", "value": 0, "value_bot": 0, "stake": "USDT", "note": ""}
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/daily",
    summary="Bot 日维度收益（代理）",
    description="请求指定 Bot 的 /api/v1/daily?timescale=...，与官方 DailyWeeklyMonthly 一致。Bot 未运行时返回空 data。",
)
def get_bot_daily_proxy(
    strategy_name: str,
    timescale: int = Query(20, ge=1, description="天数"),
):
    manager = get_manager()
    try:
        return manager.get_bot_daily(strategy_name, timescale=timescale)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/weekly",
    summary="Bot 周维度收益（代理）",
    description="请求指定 Bot 的 /api/v1/weekly?timescale=...。Bot 未运行时返回空 data。",
)
def get_bot_weekly_proxy(
    strategy_name: str,
    timescale: int = Query(20, ge=1, description="周数"),
):
    manager = get_manager()
    try:
        return manager.get_bot_weekly(strategy_name, timescale=timescale)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/monthly",
    summary="Bot 月维度收益（代理）",
    description="请求指定 Bot 的 /api/v1/monthly?timescale=...。Bot 未运行时返回空 data。",
)
def get_bot_monthly_proxy(
    strategy_name: str,
    timescale: int = Query(20, ge=1, description="月数"),
):
    manager = get_manager()
    try:
        return manager.get_bot_monthly(strategy_name, timescale=timescale)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}/logs",
    summary="Bot 日志（代理）",
    description="请求指定 Bot 的 /api/v1/logs，返回最近日志。Bot 未注册或未运行时返回空列表。",
)
def get_bot_logs_proxy(
    strategy_name: str,
    limit: Optional[int] = Query(None, description="返回条数限制"),
):
    """代理到 Bot 的 /api/v1/logs；策略未注册或未运行时返回空列表，不返回 404。"""
    manager = get_manager()
    try:
        return manager.get_bot_logs(strategy_name, limit=limit)
    except ValueError:
        return {"log_count": 0, "logs": []}
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot API 请求失败: {error}",
        ) from error


@router.get(
    "/bots/{strategy_name}",
    response_model=BotStatusResponse,
    summary="查询指定策略的 Bot 状态",
    description="返回指定策略名称的 Bot 进程详细状态。",
)
def get_bot_status(
    strategy_name: str,
    ft_config: dict = Depends(get_config),
) -> BotStatusResponse:
    manager = get_manager()
    info = manager.get_bot_status(strategy_name)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略 [{strategy_name}] 没有注册记录",
        )
    account_name = None
    if getattr(info, "account_id", None) is not None:
        accounts = load_accounts(ft_config)
        acc = next((a for a in accounts if a.get("id") == info.account_id), None)
        account_name = acc.get("name") if acc else None
    dry_run, trading_mode = _read_bot_config_dry_run_and_trading_mode(info.config_path)
    position_pairs = None
    if info.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
        try:
            trades = manager.get_bot_open_trades(strategy_name)
            position_pairs = [t.get("pair") for t in trades if isinstance(t, dict) and t.get("pair")]
        except Exception:
            pass
    return BotStatusResponse.from_process_info(
        info,
        account_name=account_name,
        dry_run=dry_run,
        trading_mode=trading_mode,
        position_pairs=position_pairs,
    )


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
    # 若有高级配置 JSON，解析后深合并到 config（用户字段覆盖基础字段）
    if request.advanced_config and request.advanced_config.strip():
        try:
            extra = json.loads(request.advanced_config.strip())
            if isinstance(extra, dict):
                config = _deep_merge(config, extra)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"高级配置不是合法 JSON：{e}",
            ) from e

    # Bot 子进程的 api_server 认证与 Manager 调用子进程 API 的凭据必须一致。
    # config 里 api_server 已写死为 "pbot"/"12300"，此处传入相同凭据存入 BotProcessInfo，供后续 _call_bot_api 使用。
    bot_api_username = (config.get("api_server") or {}).get("username", "pbot")
    bot_api_password = (config.get("api_server") or {}).get("password", "12300")
    try:
        info = manager.start_bot_with_config(
            strategy_name=request.strategy_name,
            config=config,
            api_port=request.api_port,
            api_username=bot_api_username,
            api_password=bot_api_password,
            bot_name=request.bot_name,
            account_id=request.account_id,
        )
        account_name = account.get("name") if account else None
        return OperationResponse(
            success=True,
            message=f"Bot [{request.strategy_name}] 启动成功（内存配置），PID={info.pid}",
            bot=BotStatusResponse.from_process_info(info, account_name=account_name),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
        ) from error


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深合并：override 中的键覆盖 base，递归合并嵌套 dict。"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


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
    # 文件名加当前时间戳，避免文件名冲突
    db_filename = f"{strategy_name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.dryrun.sqlite" if dry_run else f"{strategy_name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite"
    # db_filename = f"{strategy_name}.dryrun.sqlite" if dry_run else f"{strategy_name}.sqlite"
    db_url = f"sqlite:///user_data/tradebot/{db_filename}"

    base: dict[str, Any] = {
        # ── 基础运行配置 ──────────────────────────────────────────────
        "strategy": strategy_name,
        "dry_run": dry_run,
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
            "pair_whitelist": [],
            "pair_blacklist": []
        },
        # ── 定价/订单流等（exchange.validate_config 必需）──────────────
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
            "price_last_balance": 0.0,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1}
        },
        "exit_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1
        },
        "unfilledtimeout": {
            "entry": 10,
            "exit": 10,
            "exit_timeout_count": 0,
            "unit": "minutes"
        },
        "cancel_open_orders_on_exit": True,
        "trading_mode": "futures",
        "margin_mode": "isolated",
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
            # RFC 7518 建议 HS256 的 key 至少 32 字节，否则会触发 InsecureKeyLengthWarning
            "jwt_secret_key": f"pizza_bot_{strategy_name}_{request.api_port}_{secrets.token_hex(16)}",
            "CORS_origins": ["http://localhost:{request.api_port}"],
            "username": "pbot",
            "password": "12300",
        },
        # ── 数据目录 ──────────────────────────────────────────────────
        "datadir": "user_data/data",
        "user_data_dir": "user_data",
        # ── 日志 ──────────────────────────────────────────────────────
        "verbosity": 0,
        # ── 其他必要默认值 ────────────────────────────────────────────
        "bot_name": request.bot_name,
        "initial_state": "running",
        "force_entry_enable": False,
        "internals": {
            "process_throttle_secs": 5,
        },
    }
    # 仅当请求中显式传入 timeframe 时才写入 config，否则由策略类提供（官方 StrategyResolver 行为）
    if request.timeframe is not None:
        base["timeframe"] = request.timeframe
    return base


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


def _run_stop_bot(strategy_name: str, force: bool) -> None:
    """在后台线程中执行 stop_bot，避免接口长时间阻塞。"""
    manager = get_manager()
    try:
        manager.stop_bot(strategy_name, force=force)
        logger.info(f"Bot [{strategy_name}] 后台停止完成")
    except Exception as e:
        logger.exception("后台停止 Bot [%s] 失败: %s", strategy_name, e)


@router.post(
    "/bots/{strategy_name}/stop",
    response_model=OperationResponse,
    summary="停止 Bot",
    description=(
        "停止指定策略的 Bot 进程。\n\n"
        "接口立即返回 202，实际停止在后台执行（先调用 Bot API 停止，再 SIGTERM，最多等 15 秒后若未退出则 SIGKILL）。\n\n"
        "设置 `force=true` 可跳过优雅退出直接强制终止。"
    ),
    status_code=status.HTTP_202_ACCEPTED,
)
def stop_bot(
    strategy_name: str,
    request: StopBotRequest = StopBotRequest(),
) -> OperationResponse:
    manager = get_manager()
    try:
        info = manager._get_running_bot(strategy_name)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    thread = threading.Thread(
        target=_run_stop_bot,
        args=(strategy_name, request.force),
        name=f"stop_bot_{strategy_name}",
        daemon=True,
    )
    thread.start()
    return OperationResponse(
        success=True,
        message="停止已提交，正在后台执行",
        bot=BotStatusResponse.from_process_info(info),
    )


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
