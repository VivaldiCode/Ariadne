from fastapi import APIRouter
from pydantic import BaseModel

from ariadne.api.deps import PrincipalDep

router = APIRouter(tags=["identity"])


class WhoAmI(BaseModel):
    id: int
    kind: str
    name: str
    role: str  # admin | member


@router.get("/whoami")
def whoami(principal: PrincipalDep) -> WhoAmI:
    """Return the principal identified by the API key."""
    return WhoAmI(id=principal.id, kind=principal.kind, name=principal.name, role=principal.role)
