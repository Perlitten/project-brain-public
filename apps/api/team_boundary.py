from brain.config.settings import settings
from starlette._utils import get_route_path
from starlette.responses import JSONResponse


class TeamBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and settings.BRAIN_TEAM_MODE:
            await send({"type": "websocket.close", "code": 4403})
            return
        if scope["type"] == "http" and settings.BRAIN_TEAM_MODE:
            path = get_route_path(scope)
            if path == "/health":
                await JSONResponse({"status": "experimental", "mode": "team-foundations",
                                    "execution_available": False})(scope, receive, send)
                return
            if path != "/team/projects" and not path.startswith("/team/projects/") and path not in {"/mcp", "/health"}:
                await JSONResponse({"detail": "Surface unavailable in experimental team mode"}, status_code=403)(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)
