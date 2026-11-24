"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import exchange, resource

# 专用前缀路由先注册，避免被通用的 /api/{module}/{entry_id} 抢先匹配。
ROUTERS = [exchange, resource]
