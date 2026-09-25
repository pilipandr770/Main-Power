from flask_login import current_user

from ..extensions import db
from ..models import AuditLog


def audit(action: str, target: str = "", details: str = "", actor_id: int | None = None) -> None:
    if actor_id is None and current_user and getattr(current_user, "is_authenticated", False):
        actor_id = current_user.id
    db.session.add(AuditLog(actor_id=actor_id, action=action, target=target, details=details[:2000]))
