"""The server instructions carry the shopping rules an agent needs before it touches the cart"""

import pytest

from kroger_mcp.server import create_server

RULES = {
    "decide before adding": "cannot be removed from the real Kroger cart",
    "ask when options differ": "ask the user which they want",
    "weight quantities": "sold by weight",
    "unit quantities": "one per unit",
    "size arithmetic": "8 oz bag",
    "empty search": "does not mean the store does not stock",
    "alternate terms": "alternate",
    "out of stock": "TEMPORARILY_OUT_OF_STOCK",
    "check sign-in first": "get_authentication_info",
}


@pytest.mark.parametrize("rule, phrase", RULES.items())
def test_instructions_state_the_rule(rule, phrase):
    instructions = " ".join(create_server().instructions.split())
    assert phrase in instructions, rule


def test_instructions_stay_free_of_one_household(  # the repo is public
):
    instructions = create_server().instructions.lower()
    for word in ("pickup", "zip code 4", "premium"):
        assert word not in instructions
