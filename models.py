from typing import Any, Optional
from pydantic import BaseModel

class Product(BaseModel):
    product_id: int
    name: str
    category: str
    price_cents: int
    tags: list[str] = []
    description: Optional[str] = None
    attributes: dict[str, Any] = {}
    status: str = "active"