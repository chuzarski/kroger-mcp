"""bulk_search_products answers with a shortlist an agent can read, not full product records"""

import json
import time

import pytest

from kroger_mcp.tools.product_tools import _format_product
from kroger_mcp.tools.shortlist import option_line, shortlist_for_term

from conftest import BASE, call_error, call_ok

STORE = "01400513"
SEARCH_URL = f"{BASE}/v1/products"


def raw_product(product_id="0001111041700", description="2% Reduced Fat Milk", brand="Kroger", size="1 gal",
                regular=3.49, promo=None, stock="HIGH", full=False):
    item = {"size": size, "soldBy": "UNIT", "fulfillment": {"curbside": True}}
    if regular is not None:
        item["price"] = {"regular": regular}
        if promo is not None:
            item["price"]["promo"] = promo
    if stock is not None:
        item["inventory"] = {"stockLevel": stock}
    product = {"productId": product_id, "upc": product_id, "description": description, "brand": brand,
               "categories": ["Dairy"], "items": [item]}
    if full:
        product["images"] = [{"perspective": p, "sizes": [{"size": s, "url": f"https://img/{product_id}/{p}/{s}.jpg"}
                                                           for s in ("thumbnail", "small", "medium", "large")]}
                             for p in ("front", "back", "left", "right")]
        product["allergens"] = [{"name": f"Allergen {i}", "levelOfContainmentName": "Contains"} for i in range(8)]
        product["nutritionInformation"] = [{
            "ingredientStatement": "Milk, vitamin A palmitate, vitamin D3. " * 4,
            "servingSize": {"quantity": 1, "unitOfMeasure": {"abbreviation": "cup"}},
            "nutrients": [{"displayName": f"Nutrient {i}", "quantity": i, "unitOfMeasure": {"abbreviation": "g"},
                           "percentDailyIntake": i} for i in range(20)],
        }]
        product["warnings"] = "Keep refrigerated.\n" * 3
    return product


def line(**kwargs):
    return option_line(_format_product(raw_product(**kwargs)))


# option_line

def test_option_line_matches_the_documented_format():
    assert line(regular=3.49, promo=2.99) == "0001111041700 | Kroger 2% Reduced Fat Milk | 1 gal | $3.49 | sale $2.99 | HIGH"


def test_option_line_has_no_sale_segment_when_promo_is_zero_or_absent():
    expected = "0001111041700 | Kroger 2% Reduced Fat Milk | 1 gal | $3.49 | HIGH"
    assert line(promo=0) == expected
    assert line(promo=None) == expected


def test_option_line_does_not_repeat_a_brand_already_in_the_description():
    assert line(description="Kroger 2% Milk").split(" | ")[1] == "Kroger 2% Milk"


def test_option_line_shows_question_marks_for_missing_stock_and_size():
    assert line(stock=None, size=None) == "0001111041700 | Kroger 2% Reduced Fat Milk | ? | $3.49 | ?"


def test_option_line_omits_the_price_of_a_product_without_one():
    assert line(regular=None) == "0001111041700 | Kroger 2% Reduced Fat Milk | 1 gal | HIGH"


def test_option_line_of_a_product_without_items_still_renders():
    assert option_line(_format_product({"productId": "0001111041700", "description": "Milk"})) == \
        "0001111041700 | Milk | ? | ?"


# shortlist_for_term

def test_shortlist_keeps_relevance_order_and_cuts_to_three():
    products = [_format_product(raw_product(product_id=f"000111104170{i}")) for i in range(5)]
    lines, more = shortlist_for_term(products)
    assert [l.split(" | ")[0] for l in lines] == [f"000111104170{i}" for i in range(3)]
    assert more == 2


def test_shortlist_moves_out_of_stock_products_below_the_others():
    out = _format_product(raw_product(product_id="0001111041700", stock="TEMPORARILY_OUT_OF_STOCK"))
    low = _format_product(raw_product(product_id="0001111041701", stock="LOW"))
    high = _format_product(raw_product(product_id="0001111041702", stock="HIGH"))
    lines, more = shortlist_for_term([out, low, high])
    assert [l.split(" | ")[0] for l in lines] == ["0001111041701", "0001111041702", "0001111041700"]
    assert more == 0


def test_shortlist_of_nothing_is_empty():
    assert shortlist_for_term([]) == ([], 0)


# the tool

def search(terms, **extra):
    return {"searches": [{"term": t} for t in terms], "location_id": STORE, **extra}


async def test_shortlist_result_per_term(mcp, requests_mock, app_token):
    requests_mock.get(SEARCH_URL, json={"data": [raw_product(product_id=f"000111104170{i}") for i in range(5)]})
    result = await call_ok(mcp, "bulk_search_products", search(["milk"]))
    term = result["results"][0]
    assert term["term"] == "milk"
    assert term["success"] is True
    assert term["count"] == 3 and len(term["options"]) == 3 and term["more"] == 2
    assert all(isinstance(o, str) for o in term["options"])
    assert "data" not in term
    assert result["searches_successful"] == 1


async def test_an_empty_result_says_it_does_not_mean_not_stocked(mcp, requests_mock, app_token):
    requests_mock.get(SEARCH_URL, json={"data": []})
    requests_mock.head(f"{BASE}/v1/locations/{STORE}", status_code=200)
    result = await call_ok(mcp, "bulk_search_products", search(["unobtainium"]))
    term = result["results"][0]
    assert term["success"] is True
    assert term["options"] == [] and term["count"] == 0
    assert "alternate" in term["message"] and "not stocked" in term["message"]


async def test_results_keep_the_input_order_when_responses_arrive_out_of_order(mcp, requests_mock, app_token):
    def respond(request, context):
        term = request.qs["filter.term"][0]
        time.sleep({"milk": 0.4, "bread": 0.2, "eggs": 0.0}[term])
        return {"data": [raw_product(description=term.upper())]}
    requests_mock.get(SEARCH_URL, json=respond)
    result = await call_ok(mcp, "bulk_search_products", search(["milk", "bread", "eggs"]))
    assert [r["term"] for r in result["results"]] == ["milk", "bread", "eggs"]
    assert [r["options"][0].split(" | ")[1] for r in result["results"]] == ["Kroger MILK", "Kroger BREAD", "Kroger EGGS"]


async def test_a_rate_limit_stops_the_searches_that_have_not_started(mcp, requests_mock, app_token):
    def respond(request, context):
        if request.qs["filter.term"][0] == "milk":
            time.sleep(0.1)  # the first searches are all running before this one fails
            context.status_code = 429
            return {"errors": {"code": "API-RATE", "reason": "Too many requests"}}
        time.sleep(0.3)
        return {"data": [raw_product()]}
    requests_mock.get(SEARCH_URL, json=respond)
    terms = ["milk"] + [f"item{i}" for i in range(20)]
    result = await call_ok(mcp, "bulk_search_products", search(terms))
    assert result["stopped_early"]["error_type"] == "rate_limited"
    skipped = [r for r in result["results"] if r.get("searched") is False]
    assert len(skipped) >= 10
    assert all(r["success"] is False for r in skipped)


async def test_a_rate_limit_on_every_search_fails_the_call(mcp, requests_mock, app_token):
    requests_mock.get(SEARCH_URL, status_code=429, json={"errors": {"code": "API-RATE", "reason": "Too many requests"}})
    error, _ = await call_error(mcp, "bulk_search_products", search(["milk", "bread"]))
    assert error["type"] == "rate_limited"
    assert len(error["results"]) == 2


async def test_a_failed_term_does_not_stop_the_others(mcp, requests_mock, app_token):
    def respond(request, context):
        if request.qs["filter.term"][0] == "bread":
            context.status_code = 500
            return {"errors": {"code": "PRODUCT-5000", "reason": "Internal error"}}
        return {"data": [raw_product()]}
    requests_mock.get(SEARCH_URL, json=respond)
    result = await call_ok(mcp, "bulk_search_products", search(["milk", "bread", "eggs"]))
    assert [r["success"] for r in result["results"]] == [True, False, True]
    assert result["results"][1]["error"]["type"] == "server_error"


async def test_full_detail_is_limited_to_three_terms(mcp, requests_mock):
    error, _ = await call_error(mcp, "bulk_search_products", search(["milk", "bread", "eggs", "rice"], detail="full"))
    assert error["type"] == "invalid_parameter"
    assert error["param"] == "detail"
    assert "shortlist" in error["next_step"]
    assert requests_mock.call_count == 0


async def test_full_detail_returns_the_full_records(mcp, requests_mock, app_token):
    requests_mock.get(SEARCH_URL, json={"data": [raw_product(full=True)]})
    result = await call_ok(mcp, "bulk_search_products", search(["milk", "bread", "eggs"], detail="full"))
    product = result["results"][0]["data"][0]
    assert product["product_id"] == "0001111041700"
    assert "nutrition" in product and "images" in product


async def test_an_unknown_detail_value_is_rejected(mcp, requests_mock):
    error, _ = await call_error(mcp, "bulk_search_products", search(["milk"], detail="brief"))
    assert error["type"] == "invalid_arguments"


# Measured: 25 terms x 10 full-size products came to about 8,400 characters of shortlist
# (the full records of the same search are about 840,000)
SHORTLIST_BUDGET = 12_000


async def test_a_25_term_search_fits_the_character_budget(mcp, requests_mock, app_token):
    products = [raw_product(product_id=f"00011110417{i:02d}", description="Organic Whole Grain Rolled Oats Cereal",
                            full=True) for i in range(10)]
    requests_mock.get(SEARCH_URL, json={"data": products})
    result = await call_ok(mcp, "bulk_search_products", search([f"term {i}" for i in range(25)]))
    size = len(json.dumps(result))
    assert size < SHORTLIST_BUDGET, f"{size} characters"
