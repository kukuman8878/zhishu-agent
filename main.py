"""
FastAPI 应用入口

负责创建后端应用实例，注册应用生命周期函数，并把各业务模块中的 router
挂载到同一个 app 上。HTTP 请求会先进入这里创建的 app，再按路由分发到
具体的接口处理函数。
"""

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.lifespan import lifespan
from app.api.routers.health_router import health_router
from app.api.routers.query_router import query_router
from app.conf.app_config import app_config
from app.core.context import request_id_ctx_var

# lifespan 交给 FastAPI 管理，用于在服务启动和关闭时统一初始化与释放外部客户端
app = FastAPI(lifespan=lifespan)

# CORS 中间件：允许前端跨域访问，生产环境请按需收紧 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key 鉴权中间件：校验 X-API-Key 请求头，未配置 api_key 时跳过鉴权（开发模式）
class APIKeyMiddleware(BaseHTTPMiddleware):
    """中间件：通过 X-API-Key 请求头做简易 API Key 鉴权"""

    async def dispatch(self, request: Request, call_next):
        # 健康检查端点和预检请求跳过鉴权
        if request.url.path in ("/health", "/docs", "/openapi.json"):
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)

        api_key = app_config.api.api_key
        # 未配置 API_KEY 时跳过鉴权（本地开发模式）
        if not api_key:
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if provided != api_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "无效的 API Key"},
            )
        return await call_next(request)


# HTTP 中间件：为每个请求生成唯一 request_id 并写入上下文，再继续后续处理；参数 request=当前 HTTP 请求对象，call_next=指向下一层处理器/中间件的调用
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = uuid.uuid4()
    request_id_ctx_var.set(request_id)
    response = await call_next(request)
    return response


# 把查询路由和健康检查路由注册进应用
app.include_router(query_router)
app.include_router(health_router)
app.add_middleware(APIKeyMiddleware)
