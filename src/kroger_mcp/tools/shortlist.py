"""One-line product options for bulk search, so a 25-term search stays readable

An agent that has to choose between products needs the ID, name, size, price,
sale, and stock of each, not nutrition tables and image lists.
"""

from typing import Any, Dict, List, Optional, Tuple

from .shared import format_currency

# Options per term in a shortlist
SHORTLIST_SIZE = 3

OUT_OF_STOCK = "TEMPORARILY_OUT_OF_STOCK"


def _stock_level(product: Dict[str, Any]) -> Optional[str]:
    inventory = (product.get("item") or {}).get("inventory")
    return inventory.get("stockLevel") if isinstance(inventory, dict) else None


def option_line(product: Dict[str, Any]) -> str:
    """One line for a formatted product (the output of _format_product)

    Format: ID | description (with the brand, unless it is already there) |
    size | regular price | sale price, if on sale | stock level. A size or
    stock level the API did not send shows as ?; a missing price is left out.
    """
    description = product.get("description") or ""
    brand = product.get("brand")
    if brand and brand.lower() not in description.lower():
        description = f"{brand} {description}".strip()

    parts = [product.get("product_id") or "?", description, (product.get("item") or {}).get("size") or "?"]
    pricing = product.get("pricing")
    if pricing and pricing.get("regular_price") is not None:
        parts.append(format_currency(pricing["regular_price"]))
        if pricing.get("on_sale"):
            parts.append(f"sale {format_currency(pricing['sale_price'])}")
    parts.append(_stock_level(product) or "?")
    return " | ".join(parts)


def shortlist_for_term(products: List[Dict[str, Any]], top: int = SHORTLIST_SIZE) -> Tuple[List[str], int]:
    """The first `top` options of a term's formatted products, and how many were left out

    The API's relevance order is kept, except that products that are
    temporarily out of stock go after the others. A sale never changes the
    order: the agent sees it on the line and decides.
    """
    ranked = sorted(products, key=lambda product: _stock_level(product) == OUT_OF_STOCK)
    lines = [option_line(product) for product in ranked[:top]]
    return lines, len(products) - len(lines)
