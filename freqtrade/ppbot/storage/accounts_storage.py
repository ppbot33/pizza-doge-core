"""
账号信息持久化：读写 user_data/accounts.json，支持增删改查。
不依赖 RPC，仅依赖 config 中的 user_data_dir。
"""

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)

_LOCK = Lock()
_FILENAME = "accounts.json"


def _accounts_path(user_data_dir: str | Path) -> Path:
    return Path(user_data_dir) / _FILENAME


def _load_raw(user_data_dir: str | Path) -> list[dict[str, Any]]:
    path = _accounts_path(user_data_dir)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return []
    if not isinstance(data, list):
        return []
    return data


def _save_raw(user_data_dir: str | Path, items: list[dict[str, Any]]) -> None:
    path = _accounts_path(user_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def get_all(user_data_dir: str | Path) -> list[dict[str, Any]]:
    """返回所有账号列表（含前端所需默认字段）。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
    return [_enrich(a) for a in raw]


def load_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    """根据 freqtrade config 返回账号列表，供 bot_manager 等模块使用。"""
    ud = config.get("user_data_dir", "user_data")
    return get_all(ud)


def get_by_id(user_data_dir: str | Path, account_id: int) -> dict[str, Any] | None:
    """按 id 查询单条，不存在返回 None。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
    for a in raw:
        if a.get("id") == account_id:
            return _enrich(a)
    return None


def _enrich(a: dict[str, Any]) -> dict[str, Any]:
    """为前端补齐默认字段（连接状态、余额等由前端或后续接口填充）。"""
    out = dict(a)
    out.setdefault("connectionStatus", "unknown")
    out.setdefault("activeStrategies", 0)
    out.setdefault("tradingPairs", 0)
    out.setdefault("totalBalance", 0.0)
    out.setdefault("availableBalance", 0.0)
    out.setdefault("frozenBalance", 0.0)
    out.setdefault("hasRunningStrategy", False)
    out.setdefault("lastSyncTime", None)
    return out


def _next_id(items: list[dict]) -> int:
    if not items:
        return 1
    return (
        max(
            (int(x.get("id", 0)) for x in items if isinstance(x.get("id"), (int, float))), default=0
        )
        + 1
    )


def create(user_data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """新增账号，自动分配 id，返回完整记录。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
        new_id = _next_id(raw)
        record = {
            "id": new_id,
            "name": payload.get("name", ""),
            "remark": payload.get("remark", ""),
            "exchange": payload.get("exchange", "binance"),
            "apiKey": payload.get("apiKey", ""),
            "apiSecret": payload.get("apiSecret", ""),
            "maxPositions": int(payload.get("maxPositions", 10)),
            "stakeAmount": float(payload.get("stakeAmount", 100)),
            "stakeCurrency": str(payload.get("stakeCurrency", "USDT")),
        }
        raw.append(record)
        _save_raw(user_data_dir, raw)
    return _enrich(record)


def update(
    user_data_dir: str | Path, account_id: int, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """更新指定 id 的账号，返回更新后记录，不存在返回 None。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
        for i, a in enumerate(raw):
            if a.get("id") == account_id:
                if "name" in payload:
                    raw[i]["name"] = payload["name"]
                if "remark" in payload:
                    raw[i]["remark"] = payload["remark"]
                if "exchange" in payload:
                    raw[i]["exchange"] = payload["exchange"]
                if "apiKey" in payload:
                    raw[i]["apiKey"] = payload["apiKey"]
                if "apiSecret" in payload:
                    raw[i]["apiSecret"] = payload["apiSecret"]
                if "maxPositions" in payload:
                    raw[i]["maxPositions"] = int(payload["maxPositions"])
                if "stakeAmount" in payload:
                    raw[i]["stakeAmount"] = float(payload["stakeAmount"])
                if "stakeCurrency" in payload:
                    raw[i]["stakeCurrency"] = str(payload["stakeCurrency"])
                _save_raw(user_data_dir, raw)
                return _enrich(raw[i])
        return None


def delete(user_data_dir: str | Path, account_id: int) -> bool:
    """删除指定 id 的账号，存在并删除返回 True，否则 False。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
        for i, a in enumerate(raw):
            if a.get("id") == account_id:
                raw.pop(i)
                _save_raw(user_data_dir, raw)
                return True
        return False
