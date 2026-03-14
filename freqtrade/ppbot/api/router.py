"""
ppbot 聚合路由：供 webserver 以 prefix="/api/v1" 挂载，需配合 Depends(http_basic_or_jwt_token)。
"""

from fastapi import APIRouter

from freqtrade.ppbot.api.accounts import router as accounts_router
from freqtrade.ppbot.bot.api_bot_manager import router as bot_manager_router


router = APIRouter()
router.include_router(accounts_router)
router.include_router(bot_manager_router)
