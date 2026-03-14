"""
策略 CRUD 与元数据 API：列表合并 .py 与 strategies_meta.json，支持新增/修改/删除。
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from freqtrade.constants import USERPATH_STRATEGIES
from freqtrade.ppbot.storage.strategies_meta import (
    delete_meta,
    get_all_meta,
    get_meta,
    set_meta,
)
from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bot-manager/strategies", tags=["Strategies"])

# 策略名：仅允许字母数字下划线，且需为合法 Python 类名
STRATEGY_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _strategy_dir(config: dict) -> Path:
    return Path(config["user_data_dir"]) / USERPATH_STRATEGIES


def _strategy_file_path(config: dict, strategy_name: str) -> Path:
    return _strategy_dir(config) / f"{strategy_name}.py"


def _list_strategy_names_from_resolver(config: dict) -> list[str]:
    """通过官方 StrategyResolver 获取所有策略名（来自 .py 文件）。"""
    from freqtrade.resolvers.strategy_resolver import StrategyResolver

    strategies = StrategyResolver.search_all_objects(
        config, False, config.get("recursive_strategy_search", False)
    )
    return sorted([x["name"] for x in strategies])


class StrategyMetaBody(BaseModel):
    name: str = Field(..., description="策略名称（类名）")
    remark: str | None = Field(None, description="备注")
    risk_level: str | None = Field(None, description="风险等级 low/medium/high")
    exchange: str | None = Field(None, description="交易所")
    trading_pair: str | None = Field(None, description="交易对")


class StrategyCreateBody(StrategyMetaBody):
    code: str = Field(..., description="策略 Python 代码")


class StrategyUpdateBody(BaseModel):
    name: str | None = Field(None, description="策略名称（仅当重命名时，会重写 .py 文件名）")
    code: str | None = Field(None, description="策略代码")
    remark: str | None = None
    risk_level: str | None = None
    exchange: str | None = None
    trading_pair: str | None = None


@router.get("")
def list_strategies_with_meta(ft_config: dict = Depends(get_config)):
    """
    列出所有策略：合并官方解析的 .py 策略与 strategies_meta.json。
    若某策略仅有 .py 无 meta，则返回 needs_meta=True，其他字段为 NA，前端可高亮提示用户编辑保存。
    """
    names = _list_strategy_names_from_resolver(ft_config)
    meta_map = get_all_meta(ft_config["user_data_dir"])
    result = []
    for name in names:
        meta = meta_map.get(name)
        if meta:
            result.append({
                "name": name,
                "remark": meta.get("remark"),
                "risk_level": meta.get("risk_level"),
                "exchange": meta.get("exchange"),
                "trading_pair": meta.get("trading_pair"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "needs_meta": False,
            })
        else:
            result.append({
                "name": name,
                "remark": None,
                "risk_level": None,
                "exchange": None,
                "trading_pair": None,
                "created_at": None,
                "updated_at": None,
                "needs_meta": True,
            })
    return {"strategies": result}


@router.get("/{strategy_name}")
def get_strategy_meta(strategy_name: str, ft_config: dict = Depends(get_config)):
    """
    获取单条策略的元数据（供编辑页预填）。若仅有 .py 无 meta 返回 404，前端可当作“需补全”处理。
    """
    strategy_name = strategy_name.strip()
    meta = get_meta(ft_config["user_data_dir"], strategy_name)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略 [{strategy_name}] 暂无元数据，请保存一次以写入",
        )
    return meta


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(body: StrategyCreateBody, ft_config: dict = Depends(get_config)):
    """
    新增策略：将关键信息写入 strategies_meta.json，策略代码写入 user_data/strategies/{name}.py。
    """
    name = (body.name or "").strip()
    if not name or not STRATEGY_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="策略名称只能包含字母、数字、下划线，且必须以字母开头",
        )
    strategies_dir = _strategy_dir(ft_config)
    strategies_dir.mkdir(parents=True, exist_ok=True)
    py_path = _strategy_file_path(ft_config, name)
    if py_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"策略 {name} 已存在（{py_path.name}）",
        )
    if f"class {name}" not in body.code and f"class {name}(" not in body.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"代码中必须包含类定义 class {name}(...)",
        )
    py_path.write_text(body.code, encoding="utf-8")
    meta = set_meta(
        ft_config["user_data_dir"],
        name,
        {
            "remark": body.remark or "",
            "risk_level": body.risk_level or "low",
            "exchange": body.exchange or "",
            "trading_pair": body.trading_pair or "",
        },
    )
    return {"strategy": name, "meta": meta}


@router.put("/{strategy_name}")
def update_strategy(
    strategy_name: str,
    body: StrategyUpdateBody,
    ft_config: dict = Depends(get_config),
):
    """
    更新策略：可更新代码和/或元数据。若仅更新元数据（如用户从“仅 py”编辑保存），不要求传 code。
    """
    strategy_name = strategy_name.strip()
    py_path = _strategy_file_path(ft_config, strategy_name)
    if not py_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略文件不存在: {strategy_name}.py",
        )
    existing_meta = get_meta(ft_config["user_data_dir"], strategy_name) or {}
    if body.code is not None:
        name_in_code = body.name or strategy_name
        if f"class {name_in_code}" not in body.code and f"class {name_in_code}(" not in body.code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"代码中必须包含类定义 class {name_in_code}(...)",
            )
        py_path.write_text(body.code, encoding="utf-8")
    meta_update = {}
    if body.remark is not None:
        meta_update["remark"] = body.remark
    if body.risk_level is not None:
        meta_update["risk_level"] = body.risk_level
    if body.exchange is not None:
        meta_update["exchange"] = body.exchange
    if body.trading_pair is not None:
        meta_update["trading_pair"] = body.trading_pair
    merged = {**existing_meta, **meta_update}
    meta = set_meta(ft_config["user_data_dir"], strategy_name, merged)
    return {"strategy": strategy_name, "meta": meta}


@router.delete("/{strategy_name}")
def delete_strategy(strategy_name: str, ft_config: dict = Depends(get_config)):
    """
    删除策略：删除 strategies_meta.json 中的记录，并删除 user_data/strategies/{name}.py。
    """
    strategy_name = strategy_name.strip()
    py_path = _strategy_file_path(ft_config, strategy_name)
    deleted_meta = delete_meta(ft_config["user_data_dir"], strategy_name)
    deleted_file = False
    if py_path.exists():
        try:
            py_path.unlink()
            deleted_file = True
        except OSError as e:
            logger.warning("Failed to delete strategy file %s: %s", py_path, e)
    return {"strategy": strategy_name, "deleted_file": deleted_file, "deleted_meta": deleted_meta}
