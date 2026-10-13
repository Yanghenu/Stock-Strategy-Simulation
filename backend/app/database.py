"""SQL Server 驱动边界；连接参数来自配置，驱动缺失或连接失败时给出安全错误。"""

import logging
import shutil
import tempfile
from importlib.metadata import version
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from app.config import DatabaseSettings

logger = logging.getLogger(__name__)
_ODBC_STAGE_LOCK = Lock()


class DatabaseUnavailableError(RuntimeError):
    """数据库连接不可用；API 仅返回此安全消息，不返回凭据或堆栈。"""


def _prepare_odbc_package_path() -> None:
    """将含中文路径的驱动包暂存到 ASCII 临时路径，规避原生加载器路径检查。"""

    try:
        import mssql_python_odbc
    except ImportError:
        return

    source_file = Path(mssql_python_odbc.__file__)
    if str(source_file).isascii():
        return

    temp_root = Path(tempfile.gettempdir())
    if not str(temp_root).isascii():
        raise DatabaseUnavailableError("SQL Server 驱动路径含非 ASCII 字符，且系统临时目录也不是 ASCII 路径")

    package_version = "".join(char if char.isalnum() or char in ".-_" else "_" for char in version("mssql-python-odbc"))
    staged_package = temp_root / f"quantbacktest-mssql-python-odbc-{package_version}"
    driver_dir = staged_package / "libs" / "windows" / "x64"
    required_files = (driver_dir / "msodbcsql18.dll", driver_dir / "mssql-auth.dll")
    with _ODBC_STAGE_LOCK:
        if not all(path.is_file() for path in required_files):
            # Microsoft 驱动的原生路径检查无法正确识别当前中文工作区路径；只复制驱动包，不触碰配置和用户数据。
            shutil.copytree(source_file.parent, staged_package, dirs_exist_ok=True)
        if not all(path.is_file() for path in required_files):
            raise DatabaseUnavailableError("SQL Server ODBC 驱动文件暂存不完整，请检查 mssql-python-odbc 安装")
        mssql_python_odbc.__file__ = str(staged_package / source_file.name)


def create_connection(
    settings: DatabaseSettings,
    connector: Callable[..., Any] | None = None,
) -> Any:
    """创建单条 DB-API 连接并设置语句超时，连接池由 Microsoft 驱动管理。"""

    if connector is None:
        try:
            _prepare_odbc_package_path()
            from mssql_python import connect
        except ImportError as exc:
            raise DatabaseUnavailableError("缺少 mssql-python 驱动，请按 requirements.txt 安装后重试") from exc
        except DatabaseUnavailableError:
            raise
        except Exception as exc:
            logger.exception("准备 SQL Server ODBC 驱动路径失败")
            raise DatabaseUnavailableError("无法准备 SQL Server ODBC 驱动，请检查本机驱动安装") from exc
        connector = connect

    options: dict[str, object] = {
        "server": settings.server,
        "database": settings.database,
        "encrypt": "yes" if settings.encrypt else "no",
        "trust_server_certificate": "yes" if settings.trust_server_certificate else "no",
        "timeout": settings.connection_timeout_seconds,
    }
    if settings.authentication == "windows":
        options["trusted_connection"] = "yes"
    else:
        options["uid"] = settings.username
        options["pwd"] = settings.password

    try:
        connection = connector(**options)
        # 驱动的连接超时控制认证，connection.timeout 控制后续 SQL 语句。
        connection.timeout = settings.statement_timeout_seconds
        return connection
    except Exception as exc:
        logger.exception("SQL Server 连接失败")
        raise DatabaseUnavailableError("无法连接 SQL Server，请检查本机服务和 backend/config.ini") from exc
