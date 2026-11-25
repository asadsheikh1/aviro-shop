"""Synthetic shoppers: browse the catalogue, look at products, sometimes buy
and then check the order's status.

Runs in the official Locust image (`make load`). Tune with LOAD_USERS,
LOAD_SPAWN_RATE and LOAD_DURATION. About 2% of checkouts use a card that the
payments service declines, so 402s are part of normal traffic.
"""

import random
import uuid

from locust import HttpUser, between, task


class Shopper(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.skus: list[str] = []
        self.refresh_catalogue()

    def refresh_catalogue(self) -> None:
        with self.client.get("/api/products", name="/api/products", catch_response=True) as r:
            if r.status_code == 200:
                self.skus = [p["sku"] for p in r.json() if p.get("in_stock")]

    @task(5)
    def browse(self) -> None:
        self.refresh_catalogue()

    @task(3)
    def view_product(self) -> None:
        if self.skus:
            self.client.get(f"/api/products/{random.choice(self.skus)}", name="/api/products/[sku]")

    @task(1)
    def checkout(self) -> None:
        if not self.skus:
            return
        items = [
            {"sku": sku, "qty": random.randint(1, 3)}
            for sku in random.sample(self.skus, k=min(len(self.skus), random.randint(1, 3)))
        ]
        card = "tok_decline_0002" if random.random() < 0.02 else f"tok_visa_{uuid.uuid4().hex[:8]}"
        with self.client.post(
            "/api/orders",
            json={
                "customer_email": f"shopper{random.randint(1, 500)}@example.com",
                "items": items,
                "card_token": card,
            },
            name="/api/orders",
            catch_response=True,
        ) as r:
            if r.status_code in (201, 402):  # a declined card is a normal outcome
                r.success()
            if r.status_code == 201 and random.random() < 0.5:
                # Customers check their order's status after buying (needed for S11).
                order_id = r.json().get("order_id")
                if order_id:
                    self.client.get(f"/api/orders/{order_id}", name="/api/orders/[id]")
