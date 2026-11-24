"""The built-in prompts point at tools that return what they ask for"""

from conftest import call_ok  # noqa: F401  (the fixtures come from conftest)


async def test_the_store_path_prompt_asks_for_tools_that_return_aisles(mcp):
    result = await mcp.get_prompt("grocery_list_store_path", {"grocery_list": "milk, eggs"})
    text = result.messages[0].content.text
    # bulk_search_products returns a shortlist without aisle locations, unless detail is "full"
    assert "search_products" in text
    assert "aisle" in text
    assert 'detail "full"' in text or "detail='full'" in text or '"full"' in text
