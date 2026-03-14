"""
账号管理 API：GET/POST /api/v1/accounts，GET/PUT/DELETE /api/v1/accounts/{id}
数据存于 user_data/accounts.json。
"""

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from freqtrade.ppbot.storage.accounts_storage import create as storage_create
from freqtrade.ppbot.storage.accounts_storage import delete as storage_delete
from freqtrade.ppbot.storage.accounts_storage import get_all as storage_get_all
from freqtrade.ppbot.storage.accounts_storage import get_by_id as storage_get_by_id
from freqtrade.ppbot.storage.accounts_storage import update as storage_update
from freqtrade.rpc.api_server.deps import get_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["Accounts"])


def _user_data_dir(config: dict[str, Any]) -> str:
    ud = config.get("user_data_dir")
    if ud is None:
        return "user_data"
    return str(ud)


@router.get("")
def list_accounts(config=Depends(get_config)):
    """GET /api/v1/accounts - 查询当前钱包/账号列表"""
    ud = _user_data_dir(config)
    accounts = storage_get_all(ud)
    return {"accounts": accounts, "total": len(accounts)}


@router.get("/{account_id:int}")
def get_account(account_id: int, config=Depends(get_config)):
    """GET /api/v1/accounts/{id} - 查询单个账号"""
    ud = _user_data_dir(config)
    account = storage_get_by_id(ud, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.post("")
def add_account(payload: dict[str, Any] = Body(...), config=Depends(get_config)):
    """POST /api/v1/accounts - 添加账户"""
    ud = _user_data_dir(config)
    record = storage_create(ud, payload)
    return record


@router.put("/{account_id:int}")
def update_account(
    account_id: int, payload: dict[str, Any] = Body(...), config=Depends(get_config)
):
    """PUT /api/v1/accounts/{id} - 更新账户"""
    ud = _user_data_dir(config)
    record = storage_update(ud, account_id, payload)
    if record is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return record


@router.delete("/{account_id:int}")
def remove_account(account_id: int, config=Depends(get_config)):
    """DELETE /api/v1/accounts/{id} - 删除账户"""
    ud = _user_data_dir(config)
    ok = storage_delete(ud, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True}
