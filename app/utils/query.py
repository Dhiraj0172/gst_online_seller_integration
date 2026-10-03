from sqlalchemy.orm import Query

def tenant_query(model, profile_id) -> Query:
    """
    Central Tenant Query Abstraction.
    Returns a query for the model safely scoped to the specified profile_id.
    """
    return model.query.filter_by(profile_id=profile_id)
