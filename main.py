from fastapi import FastAPI
from routers import products

app = FastAPI(title="Market - Catalogue")
app.include_router(products.router)