"""
Tests for restocking API endpoints.

Expected values below are derived from server/data/demand_forecasts.json and
server/data/suppliers.json. If that data changes, these expectations must be updated.
"""
from datetime import datetime, timedelta

import pytest

from mock_data import restock_orders


@pytest.fixture(autouse=True)
def reset_restock_orders():
    """Restock orders live in module-level memory, so clear them around every test."""
    restock_orders.clear()
    yield
    restock_orders.clear()


class TestRestockRecommendations:
    """Test suite for GET /api/restock/recommendations."""

    def test_recommendations_never_exceed_budget(self, client):
        """Total cost stays within the budget and remaining budget is consistent."""
        response = client.get("/api/restock/recommendations?budget=50000")
        assert response.status_code == 200

        data = response.json()
        assert data["budget"] == 50000
        assert data["total_cost"] <= data["budget"]
        assert abs(data["remaining_budget"] - (data["budget"] - data["total_cost"])) < 0.01

    def test_recommendation_line_totals_match_quantity_times_cost(self, client):
        """Each line total equals order quantity times unit cost."""
        data = client.get("/api/restock/recommendations?budget=250000").json()

        for line in data["items"]:
            assert abs(line["line_total"] - line["order_quantity"] * line["unit_cost"]) < 0.01

    def test_recommendations_exclude_items_above_reorder_point(self, client):
        """Items with stock above their reorder point are not restocked, even if demand is high."""
        data = client.get("/api/restock/recommendations?budget=250000").json()
        skus = {line["sku"] for line in data["items"]}

        # BRG-102 (200 on hand, reorder at 120), MTR-304 (60 vs 20), VLV-506 (150 vs 100),
        # and PSU-501 (420 vs 180) are above their reorder point.
        assert "BRG-102" not in skus
        assert "MTR-304" not in skus
        assert "VLV-506" not in skus
        assert "PSU-501" not in skus

    def test_recommendations_include_items_at_or_below_reorder_point(self, client):
        """Items at or below reorder point with forecast above stock are included."""
        data = client.get("/api/restock/recommendations?budget=250000").json()
        skus = {line["sku"] for line in data["items"]}

        assert {"WDG-001", "GSK-203", "FLT-405", "SNR-420", "CTL-330"} == skus

    def test_recommendations_sorted_by_urgency(self, client):
        """Most under-stocked items (highest share of unmet forecast) come first."""
        data = client.get("/api/restock/recommendations?budget=250000").json()

        urgency = [
            line["order_quantity"] / line["forecasted_demand"] for line in data["items"]
        ]
        assert urgency == sorted(urgency, reverse=True)

    def test_recommendations_skip_lines_that_do_not_fit_but_keep_going(self, client):
        """
        With budget 8000: WDG-001 (11475) and CTL-330 (11760) do not fit and are skipped.
        GSK-203 (2340) fits, and SNR-420 (5152) fits in the remaining 5660 after it.
        The loop must continue past the skipped CTL-330 line rather than stop.
        """
        data = client.get("/api/restock/recommendations?budget=8000").json()

        assert [line["sku"] for line in data["items"]] == ["GSK-203", "SNR-420"]
        assert data["total_cost"] == 7492.0
        assert data["remaining_budget"] == 508.0

    def test_recommendations_empty_when_budget_too_small(self, client):
        """A budget below every line total yields no items and the full budget remains."""
        data = client.get("/api/restock/recommendations?budget=1000").json()

        assert data["items"] == []
        assert data["total_cost"] == 0
        assert data["remaining_budget"] == 1000

    def test_recommendation_lead_time_comes_from_supplier(self, client):
        """GSK-203 is supplied by SUP-02, which has a 21-day lead time."""
        data = client.get("/api/restock/recommendations?budget=250000").json()
        gasket = next(line for line in data["items"] if line["sku"] == "GSK-203")

        assert gasket["supplier_name"] == "Northern Electronics Co."
        assert gasket["lead_time_days"] == 21

    def test_recommendations_reject_negative_budget(self, client):
        """A negative budget is rejected by validation."""
        response = client.get("/api/restock/recommendations?budget=-1")
        assert response.status_code == 422


class TestRestockOrderCreation:
    """Test suite for POST /api/restock/orders."""

    def test_create_restock_order_success(self, client):
        """A valid order returns 201 with computed totals and status Submitted."""
        response = client.post(
            "/api/restock/orders",
            json={
                "budget": 50000,
                "items": [{"sku": "GSK-203", "quantity": 10}, {"sku": "CTL-330", "quantity": 5}],
            },
        )
        assert response.status_code == 201

        order = response.json()
        assert order["order_number"].startswith("RST-")
        assert order["status"] == "Submitted"
        assert order["total_value"] == 1115.0  # 10 * 6.50 + 5 * 210.00
        assert {item["sku"] for item in order["items"]} == {"GSK-203", "CTL-330"}

    def test_create_restock_order_ignores_client_prices(self, client):
        """Prices are taken from server data, so a client-supplied unit_cost is ignored."""
        response = client.post(
            "/api/restock/orders",
            json={
                "budget": 50000,
                "items": [{"sku": "GSK-203", "quantity": 10, "unit_cost": 0.01}],
            },
        )
        assert response.status_code == 201

        order = response.json()
        assert order["total_value"] == 65.0  # 10 * 6.50, not 10 * 0.01

    def test_create_restock_order_lead_time_is_slowest_supplier(self, client):
        """GSK-203 (SUP-02, 21 days) and CTL-330 (SUP-01, 14 days): the order takes 21 days."""
        order = client.post(
            "/api/restock/orders",
            json={
                "budget": 50000,
                "items": [{"sku": "GSK-203", "quantity": 1}, {"sku": "CTL-330", "quantity": 1}],
            },
        ).json()

        assert order["lead_time_days"] == 21
        assert order["suppliers"] == ["Northern Electronics Co.", "Pacific Components Ltd"]

    def test_create_restock_order_expected_delivery_is_submission_plus_lead_time(self, client):
        """Expected delivery is calendar days after submission, not business days."""
        before = datetime.now().replace(microsecond=0)
        order = client.post(
            "/api/restock/orders",
            json={"budget": 50000, "items": [{"sku": "CTL-330", "quantity": 2}]},
        ).json()
        after = datetime.now()

        submitted = datetime.fromisoformat(order["submitted_at"])
        expected = datetime.fromisoformat(order["expected_delivery"])

        assert before <= submitted <= after
        assert expected == submitted + timedelta(days=order["lead_time_days"])
        assert order["lead_time_days"] == 14  # CTL-330 is supplied by SUP-01

    def test_create_restock_order_over_budget_rejected(self, client):
        """An order whose total exceeds the budget is rejected with 400."""
        response = client.post(
            "/api/restock/orders",
            json={"budget": 100, "items": [{"sku": "CTL-330", "quantity": 5}]},
        )
        assert response.status_code == 400
        assert "exceeds budget" in response.json()["detail"]
        assert restock_orders == []

    def test_create_restock_order_unknown_sku_rejected(self, client):
        """A SKU that is not in the forecast data is rejected with 400."""
        response = client.post(
            "/api/restock/orders",
            json={"budget": 1000, "items": [{"sku": "NOPE-999", "quantity": 1}]},
        )
        assert response.status_code == 400
        assert "Unknown SKU" in response.json()["detail"]

    def test_create_restock_order_zero_quantity_rejected(self, client):
        """Quantities must be greater than zero (validation error from Pydantic)."""
        response = client.post(
            "/api/restock/orders",
            json={"budget": 1000, "items": [{"sku": "CTL-330", "quantity": 0}]},
        )
        assert response.status_code == 422

    def test_create_restock_order_empty_items_rejected(self, client):
        """An order with no items is rejected with 400."""
        response = client.post("/api/restock/orders", json={"budget": 1000, "items": []})
        assert response.status_code == 400
        assert "at least one item" in response.json()["detail"]

    def test_create_restock_order_duplicate_sku_rejected(self, client):
        """Listing the same SKU twice is rejected with 400."""
        response = client.post(
            "/api/restock/orders",
            json={
                "budget": 1000,
                "items": [{"sku": "CTL-330", "quantity": 1}, {"sku": "CTL-330", "quantity": 1}],
            },
        )
        assert response.status_code == 400
        assert "Duplicate SKU" in response.json()["detail"]


class TestRestockOrderListing:
    """Test suite for GET /api/restock/orders."""

    def test_list_restock_orders_empty_initially(self, client):
        """No orders are listed before any are submitted."""
        response = client.get("/api/restock/orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_restock_orders_newest_first(self, client):
        """Submitted orders are returned with the most recent first."""
        client.post("/api/restock/orders", json={"budget": 50000, "items": [{"sku": "CTL-330", "quantity": 1}]})
        second = client.post(
            "/api/restock/orders", json={"budget": 50000, "items": [{"sku": "GSK-203", "quantity": 1}]}
        ).json()

        data = client.get("/api/restock/orders").json()
        assert len(data) == 2
        assert data[0]["order_number"] == second["order_number"]

    def test_list_restock_orders_by_category(self, client):
        """Category filter keeps orders with at least one line in that category."""
        client.post("/api/restock/orders", json={"budget": 50000, "items": [{"sku": "CTL-330", "quantity": 1}]})
        client.post("/api/restock/orders", json={"budget": 50000, "items": [{"sku": "GSK-203", "quantity": 1}]})

        data = client.get("/api/restock/orders?category=Controllers").json()
        assert len(data) == 1
        assert data[0]["items"][0]["sku"] == "CTL-330"

    def test_list_restock_orders_by_month(self, client):
        """Period filter matches the submission month."""
        client.post("/api/restock/orders", json={"budget": 50000, "items": [{"sku": "CTL-330", "quantity": 1}]})
        current_month = datetime.now().strftime("%Y-%m")

        assert len(client.get(f"/api/restock/orders?month={current_month}").json()) == 1
        assert client.get("/api/restock/orders?month=2020-01").json() == []

    def test_list_restock_orders_all_filters_are_noops(self, client):
        """The 'all' value skips the filter, matching the other endpoints."""
        client.post("/api/restock/orders", json={"budget": 50000, "items": [{"sku": "CTL-330", "quantity": 1}]})

        data = client.get("/api/restock/orders?category=all&month=all").json()
        assert len(data) == 1
