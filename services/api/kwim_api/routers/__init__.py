"""One module per contract surface, each exporting a single `router`.

The name is `router` in every module, never the surface name: a submodule and a
package-level symbol sharing a name (`kwim_api.routers.memory` the module vs
`memory` the APIRouter) shadow each other, and which one wins depends on import
order.
"""
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
)
