"""
Bot Process Manager
===================
独立的进程管理模块，用于通过 Python 控制多个 Freqtrade 交易 Bot 进程。

特性：
- 支持同时管理多个策略的独立 Bot 进程
- 同一策略同时只能运行一个进程
- 支持启动、暂停（stopentry）、停止操作
- 进程状态持久化到 JSON 文件，管理服务重启后可恢复状态感知
- 日志按策略独立输出
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

# ─── 基准目录 ──────────────────────────────────────────────────────────────────
# 本文件位于 pizza-doge-core/freqtrade/ppbot/bot/bot_process_manager.py
# 上溯 4 级（bot → ppbot → freqtrade → pizza-doge-core）即为项目根目录
_PCSMANAGE_DIR = Path(__file__).parent  # pizza-doge-core/freqtrade/ppbot/bot/
_PPBOT_DIR = _PCSMANAGE_DIR.parent  # pizza-doge-core/freqtrade/ppbot
_FREQTRADE_DIR = _PPBOT_DIR.parent  # pizza-doge-core/freqtrade/
_PROJECT_ROOT = _FREQTRADE_DIR.parent  # pizza-doge-core/

# 进程状态持久化目录和文件路径（存放在 user_data/tradebot/ 下，运行时自动创建目录）
_TRADEBOT_DIR = _PROJECT_ROOT / "user_data" / "tradebot"
_TRADEBOT_DIR.mkdir(parents=True, exist_ok=True)
PROCESS_STATE_FILE = _TRADEBOT_DIR / "bot_processes.json"

# Bot 进程的可能状态
BOT_STATUS_RUNNING = "running"
BOT_STATUS_PAUSED = "paused"  # stopentry 状态：不开新仓，管理已有仓位
BOT_STATUS_STOPPED = "stopped"
BOT_STATUS_ERROR = "error"


@dataclass
class BotProcessInfo:
    """单个 Bot 进程的信息"""

    strategy_name: str
    config_path: str
    pid: Optional[int] = None
    status: str = BOT_STATUS_STOPPED
    api_port: Optional[int] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    log_file: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotProcessInfo":
        return cls(**data)


class BotProcessManager:
    """
    Bot 进程管理器

    负责管理多个 Freqtrade Bot 进程的生命周期，包括：
    - 启动：为指定策略启动一个独立的 Bot 进程
    - 暂停：通过 Freqtrade API 发送 stopentry 指令（停止开仓，保留已有仓位管理）
    - 停止：终止 Bot 进程
    - 状态查询：查询所有或指定策略的运行状态
    """

    def __init__(self, freqtrade_executable: Optional[str] = None, logs_dir: Optional[str] = None):
        """
        初始化进程管理器

        :param freqtrade_executable: freqtrade 可执行文件路径，默认自动检测
        :param logs_dir: Bot 日志输出目录，默认为 pizza-doge-core/user_data/logs/bot_manager/
        """
        self._processes: dict[str, BotProcessInfo] = {}
        self._freqtrade_executable = freqtrade_executable or self._detect_freqtrade_executable()

        # 日志目录：默认存放在项目根目录的 user_data/logs/bot_manager/ 下
        self._logs_dir = (
            Path(logs_dir) if logs_dir else _PROJECT_ROOT / "user_data" / "logs" / "bot_manager"
        )
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        # 从持久化文件恢复状态
        self._load_state()
        # 校验已记录进程的实际存活状态
        self._sync_process_status()

    # ─── 可执行文件检测 ────────────────────────────────────────────────────────

    def _detect_freqtrade_executable(self) -> str:
        """自动检测 freqtrade 可执行文件路径"""
        python_executable = sys.executable
        freqtrade_module_entry = [python_executable, "-m", "freqtrade"]

        # 优先使用当前 Python 环境中的 freqtrade 模块（-m 方式）
        try:
            result = subprocess.run(
                freqtrade_module_entry + ["--version"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(_PROJECT_ROOT),
            )
            if result.returncode == 0:
                logger.info(f"使用 Python 模块方式运行 freqtrade: {python_executable} -m freqtrade")
                return "__python_module__"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # 回退到系统 PATH 中的 freqtrade 命令
        logger.info("使用系统 PATH 中的 freqtrade 命令")
        return "freqtrade"

    def _build_command(self, subcommand: str, extra_args: list[str]) -> list[str]:
        """构建 freqtrade 命令"""
        if self._freqtrade_executable == "__python_module__":
            return [sys.executable, "-m", "freqtrade", subcommand] + extra_args
        return [self._freqtrade_executable, subcommand] + extra_args

    # ─── 状态持久化 ────────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        """将当前进程状态持久化到 JSON 文件"""
        state = {name: info.to_dict() for name, info in self._processes.items()}
        try:
            with open(PROCESS_STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
        except OSError as error:
            logger.warning(f"保存进程状态失败: {error}")

    def _load_state(self) -> None:
        """从 JSON 文件恢复进程状态"""
        if not PROCESS_STATE_FILE.exists():
            return
        try:
            with open(PROCESS_STATE_FILE, "r", encoding="utf-8") as file:
                state = json.load(file)
            for name, data in state.items():
                self._processes[name] = BotProcessInfo.from_dict(data)
            logger.info(f"从持久化文件恢复了 {len(self._processes)} 个 Bot 记录")
        except (OSError, json.JSONDecodeError, TypeError) as error:
            logger.warning(f"加载进程状态失败: {error}")

    def _sync_process_status(self) -> None:
        """
        校验已记录进程的实际存活状态。
        管理服务重启后，通过 PID 检查进程是否仍在运行。
        """
        for name, info in self._processes.items():
            if info.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED) and info.pid:
                if not self._is_process_alive(info.pid):
                    logger.info(f"Bot [{name}] PID={info.pid} 已不存在，更新状态为 stopped")
                    info.status = BOT_STATUS_STOPPED
                    info.pid = None
        self._save_state()

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """检查指定 PID 的进程是否存活"""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # ─── 核心操作 ──────────────────────────────────────────────────────────────

    def start_bot(self, strategy_name: str, config_path: str) -> BotProcessInfo:
        """
        启动指定策略的 Bot 进程。
        同一策略同时只能运行一个进程，重复启动会抛出异常。

        :param strategy_name: 策略类名，如 "MyStrategy"
        :param config_path: 配置文件路径（绝对路径或相对于 pizza-doge-core/ 的路径）
        :return: BotProcessInfo
        :raises ValueError: 策略已在运行时
        :raises FileNotFoundError: 配置文件不存在时
        """
        # 检查是否已有运行中的进程
        existing = self._processes.get(strategy_name)
        if existing and existing.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
            if existing.pid and self._is_process_alive(existing.pid):
                raise ValueError(
                    f"策略 [{strategy_name}] 已有运行中的 Bot 进程 (PID={existing.pid})，"
                    f"请先停止后再启动。"
                )

        # 验证配置文件存在（相对路径以项目根目录为基准）
        config_file = Path(config_path)
        if not config_file.is_absolute():
            config_file = _PROJECT_ROOT / config_path
        if not config_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_file}")

        # 读取配置文件获取 api_port
        api_port = self._read_api_port_from_config(config_file)

        # 准备日志文件
        log_file = (
            self._logs_dir / f"{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )

        # 构建启动命令
        command = self._build_command(
            "trade",
            [
                "--config",
                str(config_file),
                "--strategy",
                strategy_name,
                "--logfile",
                str(log_file),
            ],
        )

        logger.info(f"启动 Bot [{strategy_name}]: {' '.join(command)}")

        try:
            log_file_handle = open(log_file, "a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                stdout=log_file_handle,
                stderr=log_file_handle,
                # 工作目录设为项目根目录，与命令行直接启动保持一致
                cwd=str(_PROJECT_ROOT),
                start_new_session=True,  # 子进程独立会话，父进程退出不影响子进程
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"启动 Bot [{strategy_name}] 失败: {error}") from error

        # 等待短暂时间确认进程未立即崩溃
        time.sleep(1.5)
        if process.poll() is not None:
            error_output = ""
            try:
                with open(log_file, "r", encoding="utf-8") as log_read:
                    error_output = log_read.read()[-500:]
            except OSError:
                pass
            raise RuntimeError(
                f"Bot [{strategy_name}] 启动后立即退出 (exit_code={process.returncode})。"
                f"日志末尾: {error_output}"
            )

        info = BotProcessInfo(
            strategy_name=strategy_name,
            config_path=str(config_file),
            pid=process.pid,
            status=BOT_STATUS_RUNNING,
            api_port=api_port,
            started_at=datetime.now().isoformat(),
            log_file=str(log_file),
        )
        self._processes[strategy_name] = info
        self._save_state()

        logger.info(f"Bot [{strategy_name}] 启动成功，PID={process.pid}")
        return info

    def start_bot_with_config(
        self, strategy_name: str, config: dict, api_port: int
    ) -> BotProcessInfo:
        """
        通过内存中的 config dict 启动 Bot 进程（方案 A：环境变量传参）。
        配置不落盘，API Key 等敏感信息仅存在于子进程的环境变量中。

        原有的 start_bot()（基于配置文件）保持不变，两种方式互不影响。

        :param strategy_name: 策略类名，如 "MyStrategy"
        :param config: 完整的 Freqtrade config dict，由调用方在内存中构建
        :param api_port: Bot 的 api_server 监听端口，用于后续暂停/恢复操作
        :return: BotProcessInfo
        :raises ValueError: 策略已在运行时
        :raises RuntimeError: 子进程启动失败或立即崩溃时
        """
        # 检查是否已有运行中的进程
        existing = self._processes.get(strategy_name)
        if existing and existing.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
            if existing.pid and self._is_process_alive(existing.pid):
                raise ValueError(
                    f"策略 [{strategy_name}] 已有运行中的 Bot 进程 (PID={existing.pid})，"
                    f"请先停止后再启动。"
                )

        # 准备日志文件
        log_file = (
            self._logs_dir / f"{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )

        # 构建子进程命令：使用 bot_runner.py 作为入口，而非 freqtrade trade 命令
        runner_script = str(_PCSMANAGE_DIR / "bot_runner.py")
        command = [sys.executable, runner_script]

        # 将 config dict 序列化为 JSON，通过环境变量传入子进程
        # 每次 Popen 都使用独立的 env 副本，多个 Bot 之间完全隔离
        child_env = os.environ.copy()
        child_env["FREQTRADE_CONFIG_JSON"] = json.dumps(config, ensure_ascii=False)

        logger.info(f"以内存配置方式启动 Bot [{strategy_name}]，api_port={api_port}")

        try:
            log_file_handle = open(log_file, "a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                stdout=log_file_handle,
                stderr=log_file_handle,
                env=child_env,
                cwd=str(_PROJECT_ROOT),
                start_new_session=True,  # 子进程独立会话，父进程退出不影响子进程
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"启动 Bot [{strategy_name}] 失败: {error}") from error

        # 等待短暂时间确认进程未立即崩溃
        time.sleep(1.5)
        if process.poll() is not None:
            error_output = ""
            try:
                with open(log_file, "r", encoding="utf-8") as log_read:
                    error_output = log_read.read()[-500:]
            except OSError:
                pass
            raise RuntimeError(
                f"Bot [{strategy_name}] 启动后立即退出 (exit_code={process.returncode})。"
                f"日志末尾: {error_output}"
            )

        info = BotProcessInfo(
            strategy_name=strategy_name,
            config_path="<内存配置，无配置文件>",
            pid=process.pid,
            status=BOT_STATUS_RUNNING,
            api_port=api_port,
            started_at=datetime.now().isoformat(),
            log_file=str(log_file),
        )
        self._processes[strategy_name] = info
        self._save_state()

        logger.info(f"Bot [{strategy_name}] 启动成功（内存配置），PID={process.pid}")
        return info

    def pause_bot(self, strategy_name: str) -> BotProcessInfo:
        """
        暂停指定策略的 Bot（停止开新仓，继续管理已有仓位）。
        通过调用 Bot 自身的 /api/v1/pause 接口实现。

        :param strategy_name: 策略类名
        :return: BotProcessInfo
        :raises ValueError: Bot 未运行时
        :raises RuntimeError: 无法连接 Bot API 时
        """
        info = self._get_running_bot(strategy_name)

        if info.status == BOT_STATUS_PAUSED:
            logger.info(f"Bot [{strategy_name}] 已处于暂停状态")
            return info

        if not info.api_port:
            raise RuntimeError(
                f"Bot [{strategy_name}] 未配置 api_server 端口，无法发送暂停指令。"
                f"请在配置文件中启用 api_server 并设置 listen_port。"
            )

        self._call_bot_api(info, "POST", "/api/v1/pause")

        info.status = BOT_STATUS_PAUSED
        self._save_state()
        logger.info(f"Bot [{strategy_name}] 已暂停（stopentry）")
        return info

    def resume_bot(self, strategy_name: str) -> BotProcessInfo:
        """
        恢复暂停的 Bot，重新允许开新仓。
        通过调用 Bot 自身的 /api/v1/start 接口实现。

        :param strategy_name: 策略类名
        :return: BotProcessInfo
        :raises ValueError: Bot 未运行时
        """
        info = self._get_running_bot(strategy_name)

        if info.status == BOT_STATUS_RUNNING:
            logger.info(f"Bot [{strategy_name}] 已处于运行状态")
            return info

        if not info.api_port:
            raise RuntimeError(f"Bot [{strategy_name}] 未配置 api_server 端口，无法发送恢复指令。")

        self._call_bot_api(info, "POST", "/api/v1/start")

        info.status = BOT_STATUS_RUNNING
        self._save_state()
        logger.info(f"Bot [{strategy_name}] 已恢复运行")
        return info

    def stop_bot(self, strategy_name: str, force: bool = False) -> BotProcessInfo:
        """
        停止指定策略的 Bot 进程。
        优先尝试优雅退出（SIGTERM），超时后强制终止（SIGKILL）。

        :param strategy_name: 策略类名
        :param force: 是否跳过优雅退出直接强制终止
        :return: BotProcessInfo
        :raises ValueError: Bot 未运行时
        """
        info = self._get_running_bot(strategy_name)

        pid = info.pid
        if not pid:
            info.status = BOT_STATUS_STOPPED
            self._save_state()
            return info

        if not self._is_process_alive(pid):
            info.status = BOT_STATUS_STOPPED
            info.pid = None
            info.stopped_at = datetime.now().isoformat()
            self._save_state()
            return info

        if not force:
            # 优先通过 Bot API 优雅停止
            if info.api_port:
                try:
                    self._call_bot_api(info, "POST", "/api/v1/stop")
                    time.sleep(2)
                except Exception as api_error:
                    logger.warning(
                        f"Bot [{strategy_name}] API 停止失败，将使用信号终止: {api_error}"
                    )

            # 发送 SIGTERM 优雅退出
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Bot [{strategy_name}] 已发送 SIGTERM (PID={pid})")
            except ProcessLookupError:
                pass

            # 等待最多 15 秒优雅退出
            for _ in range(15):
                time.sleep(1)
                if not self._is_process_alive(pid):
                    break

        # 如果进程仍存活，强制终止
        if self._is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Bot [{strategy_name}] 已强制终止 (SIGKILL, PID={pid})")
            except ProcessLookupError:
                pass

        info.status = BOT_STATUS_STOPPED
        info.pid = None
        info.stopped_at = datetime.now().isoformat()
        self._save_state()
        logger.info(f"Bot [{strategy_name}] 已停止")
        return info

    def get_bot_status(self, strategy_name: str) -> Optional[BotProcessInfo]:
        """
        获取指定策略的 Bot 状态。

        :param strategy_name: 策略类名
        :return: BotProcessInfo 或 None（未注册时）
        """
        info = self._processes.get(strategy_name)
        if info and info.pid and info.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
            if not self._is_process_alive(info.pid):
                info.status = BOT_STATUS_STOPPED
                info.pid = None
                self._save_state()
        return info

    def list_all_bots(self) -> list[BotProcessInfo]:
        """
        列出所有已注册的 Bot 信息（包括已停止的历史记录）。

        :return: BotProcessInfo 列表
        """
        for info in self._processes.values():
            if info.pid and info.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
                if not self._is_process_alive(info.pid):
                    info.status = BOT_STATUS_STOPPED
                    info.pid = None
        self._save_state()
        return list(self._processes.values())

    def remove_bot_record(self, strategy_name: str) -> None:
        """
        从注册表中删除已停止的 Bot 记录。
        只能删除状态为 stopped/error 的记录。

        :param strategy_name: 策略类名
        :raises ValueError: Bot 仍在运行时
        """
        info = self._processes.get(strategy_name)
        if not info:
            raise ValueError(f"策略 [{strategy_name}] 没有注册记录")
        if info.status in (BOT_STATUS_RUNNING, BOT_STATUS_PAUSED):
            raise ValueError(f"策略 [{strategy_name}] 仍在运行，请先停止后再删除记录")
        del self._processes[strategy_name]
        self._save_state()

    # ─── 内部辅助方法 ──────────────────────────────────────────────────────────

    def _get_running_bot(self, strategy_name: str) -> BotProcessInfo:
        """获取运行中的 Bot，不存在或已停止则抛出异常"""
        info = self._processes.get(strategy_name)
        if not info:
            raise ValueError(f"策略 [{strategy_name}] 没有注册记录，请先启动")
        if info.status == BOT_STATUS_STOPPED:
            raise ValueError(f"策略 [{strategy_name}] 当前未运行")
        if info.pid and not self._is_process_alive(info.pid):
            info.status = BOT_STATUS_STOPPED
            info.pid = None
            self._save_state()
            raise ValueError(f"策略 [{strategy_name}] 进程已意外退出")
        return info

    def _read_api_port_from_config(self, config_path: Path) -> Optional[int]:
        """从配置文件中读取 api_server 的监听端口"""
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)
            api_server_config = config.get("api_server", {})
            if api_server_config.get("enabled", False):
                return api_server_config.get("listen_port")
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"读取配置文件 {config_path} 失败: {error}")
        return None

    def _read_api_credentials_from_config(self, config_path: str) -> tuple[str, str]:
        """从配置文件中读取 api_server 的用户名和密码"""
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)
            api_server_config = config.get("api_server", {})
            username = api_server_config.get("username", "freqtrade")
            password = api_server_config.get("password", "")
            return username, password
        except (OSError, json.JSONDecodeError):
            return "freqtrade", ""

    def _call_bot_api(self, info: BotProcessInfo, method: str, endpoint: str) -> dict:
        """
        调用 Bot 自身的 REST API。
        使用 HTTP Basic Auth 认证。

        :param info: BotProcessInfo
        :param method: HTTP 方法（GET/POST）
        :param endpoint: API 路径，如 "/api/v1/start"
        :return: 响应 JSON
        :raises RuntimeError: 请求失败时
        """
        import base64
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{info.api_port}{endpoint}"
        username, password = self._read_api_credentials_from_config(info.config_path)
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()

        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
            },
            data=b"{}",
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read()
                return json.loads(response_body) if response_body else {}
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"调用 Bot API {url} 失败: {error}。"
                f"请确认 Bot 的 api_server 已启用且端口 {info.api_port} 可访问。"
            ) from error


# ─── 全局单例 ──────────────────────────────────────────────────────────────────

_manager_instance: Optional[BotProcessManager] = None


def get_manager() -> BotProcessManager:
    """获取全局 BotProcessManager 单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = BotProcessManager()
    return _manager_instance
