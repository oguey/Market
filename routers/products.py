from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from database import catalog
from models import Product

router = APIRouter(prefix="/products", tags=["products"])

@router.post("", status_code=201)
def create(p: Product):
    doc = p.model_dump()
    doc["indexed_at"] = datetime.now(timezone.utc)
    try:
        catalog.insert_one(doc)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"product_id": p.product_id}

@router.get("")
def list_all():
    return list(catalog.find({}, {"_id": 0}))

@router.get("/{product_id}")
def get_one(product_id: int):
    doc = catalog.find_one({"product_id": product_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Produit introuvable")
    return doc

@router.put("/{product_id}")
def update(product_id: int, p: Product):
    res = catalog.update_one({"product_id": product_id}, {"$set": p.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Produit introuvable")
    return {"updated": product_id}

@router.delete("/{product_id}")
def delete(product_id: int):
    res = catalog.delete_one({"product_id": product_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Produit introuvable")
    return {"deleted": product_id}