import os

MAX_BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "F9LHodD0cOIGVmZjP92Cx5aPIU3J4RsgeOfyouCWj3mhYdovPvjhSr8wX_OnK2O4zLqu7s4JoKAbi-60PV4i",
)

API_URL = "https://platform-api.max.ru"

os.makedirs("data", exist_ok=True)
DB_NAME = "data/max_planner.db"
