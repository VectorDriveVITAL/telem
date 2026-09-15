from fastapi import APIRouter

from .. import generator
from ..models import Incident

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=list[Incident], summary="Incidents (alerts grouped by source)")
def list_incidents():
    """Alerts for the same source grouped into a single incident with open/resolved state -
    an incident opens on the first warning/critical alert for a source and resolves when
    that source returns to healthy."""
    return generator.get_incidents()
