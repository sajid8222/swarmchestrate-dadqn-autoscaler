"""Online Boutique workload generator — CLUSTER-2 TUNED.

Same task mix as wiki_locustfile.py but:
  - wait_time = between(1, 5) seconds (instead of fixed 5s)
  - drives more aggressive RPS per user, matches user's prior cluster-1 settings

Use with workloads/rps-c2.csv which ramps to a 500-user peak (vs the 840 in rps-200.csv).
"""
import os
import random

import pandas as pd
from locust import HttpUser, LoadTestShape, TaskSet, between

WORKLOAD_CSV = os.environ.get("WORKLOAD_CSV", "workloads/rps-c2.csv")
DURATION_S = int(os.environ.get("DURATION_S", "600"))
SCALE_FACTOR = float(os.environ.get("SCALE_FACTOR", "1"))
SPAWN_RATE = int(os.environ.get("SPAWN_RATE", "10"))

PRODUCTS = [
    "0PUK6V6EV0", "1YMWWN1N4O", "2ZYFJ3GM2N", "66VCHSJNUP", "6E92ZMYYFZ",
    "9SIQT8TOJO", "L9ECAV7KIM", "LS4PSXUNUM", "OLJCESPC7Z",
]


def index(l): l.client.get("/")
def setCurrency(l):
    l.client.post("/setCurrency", {"currency_code": random.choice(["EUR", "USD", "JPY", "CAD"])})
def browseProduct(l): l.client.get("/product/" + random.choice(PRODUCTS))
def viewCart(l): l.client.get("/cart")
def addToCart(l):
    p = random.choice(PRODUCTS)
    l.client.get("/product/" + p)
    l.client.post("/cart", {"product_id": p, "quantity": random.choice([1, 2, 3, 5, 10])})
def checkout(l):
    addToCart(l)
    l.client.post("/cart/checkout", {
        "email": "someone@example.com", "street_address": "1600 Amphitheatre Parkway",
        "zip_code": "94043", "city": "Mountain View", "state": "CA", "country": "United States",
        "credit_card_number": "4432-8015-6152-0454", "credit_card_expiration_month": "1",
        "credit_card_expiration_year": "2039", "credit_card_cvv": "672",
    })


class UserBehavior(TaskSet):
    def on_start(self): index(self)
    tasks = {index: 1, setCurrency: 1, browseProduct: 10,
             addToCart: 2, viewCart: 3, checkout: 1}


class WebsiteUser(HttpUser):
    tasks = [UserBehavior]
    wait_time = between(1, 5)


class CSVDrivenShape(LoadTestShape):
    wave_df = pd.read_csv(WORKLOAD_CSV)
    wave_list = wave_df["count"].tolist()
    window_num = len(wave_list)
    window_size = DURATION_S / max(window_num, 1)

    stages = []
    for i, count in enumerate(wave_list):
        users = int(count * SCALE_FACTOR)
        stages.append({"duration": int((i + 1) * window_size),
                       "users": users, "spawn_rate": SPAWN_RATE})

    def tick(self):
        run_time = round(self.get_run_time())
        for stage in self.stages:
            if run_time <= stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None
