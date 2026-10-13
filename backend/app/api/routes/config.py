"""前端只读安全配置接口。"""

from fastapi import APIRouter, Request

from app.config import public_config

router = APIRouter()


@router.get("/app-config")
def get_app_config(request: Request) -> dict[str, object]:
    return public_config(request.app.state.settings)
