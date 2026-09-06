from fastapi import APIRouter

from .. import generator
from ..models import Alert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("", response_model=list[Alert])
def list_alerts(limit: int = 20):
    return generator.get_alerts(limit=max(1, min(limit, 200)))
