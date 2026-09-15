from fastapi import APIRouter, Query

from .. import generator
from ..models import Alert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=list[Alert], summary="Recent alert feed")
def list_alerts(limit: int = Query(20, description="Max number of alerts to return (1-200), most recent first.")):
    """Edge-triggered alerts derived from real threshold crossings on service/buoy data -
    fires on a status change, not continuously."""
    return generator.get_alerts(limit=max(1, min(limit, 200)))
