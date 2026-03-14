"""
策略元数据持久化：读写 user_data/strategies/strategies_meta.json。
与策略 .py 文件分离存储，便于在用户直接拷贝 py 时仍能列出策略，缺失 meta 时由前端高亮提示。
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from freqtrade.constants import USERPATH_STRATEGIES


logger = logging.getLogger(__name__)

_LOCK = Lock()
_FILENAME = "strategies_meta.json"


def _meta_path(user_data_dir: str | Path) -> Path:
    return Path(user_data_dir) / USERPATH_STRATEGIES / _FILENAME


def _load_raw(user_data_dir: str | Path) -> dict[str, dict[str, Any]]:
    path = _meta_path(user_data_dir)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_raw(user_data_dir: str | Path, data: dict[str, dict[str, Any]]) -> None:
    path = _meta_path(user_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_all_meta(user_data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """返回所有策略的元数据 { strategy_name: meta_dict }。"""
    with _LOCK:
        return _load_raw(user_data_dir).copy()


def get_meta(user_data_dir: str | Path, strategy_name: str) -> dict[str, Any] | None:
    """返回指定策略的元数据，不存在返回 None。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
    return raw.get(strategy_name)


def set_meta(
    user_data_dir: str | Path,
    strategy_name: str,
    meta: dict[str, Any],
    *,
    create_only: bool = False,
) -> dict[str, Any]:
    """
    写入或更新策略元数据。meta 中 name 会强制为 strategy_name。
    若 create_only=True 且已存在则不做任何修改并返回当前值。
    返回写入后的完整 meta（含 name、created_at、updated_at 等）。
    """
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        raw = _load_raw(user_data_dir)
        existing = raw.get(strategy_name)
        if create_only and existing:
            return dict(existing)
        record = dict(meta)
        record["name"] = strategy_name
        record["updated_at"] = now
        if not existing:
            record["created_at"] = now
        else:
            record["created_at"] = existing.get("created_at") or now
        raw[strategy_name] = record
        _save_raw(user_data_dir, raw)
    return record


def delete_meta(user_data_dir: str | Path, strategy_name: str) -> bool:
    """删除指定策略的元数据，存在并删除返回 True。"""
    with _LOCK:
        raw = _load_raw(user_data_dir)
        if strategy_name not in raw:
            return False
        del raw[strategy_name]
        _save_raw(user_data_dir, raw)
    return True
