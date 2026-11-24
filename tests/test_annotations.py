"""Every tool tells clients whether it only reads, and whether it destroys anything"""

import pytest

READ_ONLY = {
    "search_locations", "get_user_zip_code", "get_location_details", "get_preferred_location",
    "check_location_exists", "get_product_images", "search_products", "bulk_search_products",
    "get_product_details", "search_products_by_id", "view_current_cart", "view_order_history",
    "list_chains", "get_chain_details", "check_chain_exists", "list_departments",
    "get_department_details", "check_department_exists", "get_user_profile", "test_authentication",
    "get_authentication_info", "get_current_datetime",
}
DESTRUCTIVE = {"remove_from_cart", "clear_current_cart", "force_reauthenticate"}


async def _annotations(mcp):
    return {tool.name: tool.annotations for tool in await mcp.list_tools()}


async def test_every_tool_has_explicit_annotations(mcp):
    for name, annotations in (await _annotations(mcp)).items():
        assert annotations is not None, name
        assert isinstance(annotations.read_only_hint, bool), f"{name}: read_only_hint"
        assert isinstance(annotations.destructive_hint, bool), f"{name}: destructive_hint"


async def test_read_only_tools_are_marked_read_only_and_not_destructive(mcp):
    annotations = await _annotations(mcp)
    assert READ_ONLY <= set(annotations)
    for name in READ_ONLY:
        assert annotations[name].read_only_hint is True, name
        assert annotations[name].destructive_hint is False, name


async def test_cart_clearing_and_sign_out_tools_are_destructive(mcp):
    annotations = await _annotations(mcp)
    for name in DESTRUCTIVE:
        assert annotations[name].read_only_hint is False, name
        assert annotations[name].destructive_hint is True, name


async def test_every_other_tool_changes_state_without_destroying(mcp):
    annotations = await _annotations(mcp)
    for name in set(annotations) - READ_ONLY - DESTRUCTIVE:
        assert annotations[name].read_only_hint is False, name
        assert annotations[name].destructive_hint is False, name
