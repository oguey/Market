import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

_pwd = quote_plus(os.getenv("CATALOG_DB_PASSWORD", ""))
MONGO_URI = f"mongodb://catalog_svc:{_pwd}@localhost:27017/market?authSource=market"

client = MongoClient(MONGO_URI)
db = client["market"]
catalog = db["catalog"]