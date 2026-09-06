from fastapi import APIRouter

from .. import generator
from ..models import Incident

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=list[Incident])
def list_incidents():
    return generator.get_incidents()
