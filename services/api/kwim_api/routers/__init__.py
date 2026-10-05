"""One module per contract surface, each exporting `router`. The name is never
the surface name, which would shadow the submodule of the same name.
"""
from .admin import router as admin_router
from .admin_read import router as admin_read_router
from .admin_teams import router as admin_teams_router
from .admin_write import router as admin_write_router
from .code import router as code_router
from .knowledge import router as knowledge_router
from .memory import router as memory_router
from .proposals import router as proposals_router
from .review import router as review_router
from .wisdom import router as wisdom_router

# Mount order.
ALL_ROUTERS = (
    knowledge_router,
    wisdom_router,
    memory_router,
    proposals_router,
    review_router,
    code_router,
    admin_router,
    admin_read_router,
    admin_write_router,
    admin_teams_router,
)
