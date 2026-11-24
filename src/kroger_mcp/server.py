#!/usr/bin/env python3
"""
FastMCP Server for Kroger API

This server provides MCP tools for interacting with the Kroger API, including:
- Location management (search stores, get details, set preferred location)
- Product search and details
- Cart management (add items, bulk operations, tracking)
- Chain and department information
- User profile and authentication

Environment Variables Required:
- KROGER_CLIENT_ID: Your Kroger API client ID
- KROGER_CLIENT_SECRET: Your Kroger API client secret
- KROGER_REDIRECT_URI: Redirect URI for OAuth2 flow (default: http://localhost:8000/callback)
- KROGER_USER_ZIP_CODE: Default zip code for location searches (optional)
"""

import asyncio
import contextlib
import logging

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse

from kroger_api.utils.env import load_env_file

# Import all tool modules
from .tools import location_tools
from .tools import product_tools
from .tools import cart_tools
from .tools import info_tools
from .tools import profile_tools
from .tools import utility_tools
from .tools import auth_tools

# Import prompts
from . import prompts
from .errors import ToolErrorMiddleware
from .tools.signin_status import status as signin_status
from .tools.token_refresh import refresh_interval, run_refresh_loop

logger = logging.getLogger(__name__)


async def _run_refresh_loop(interval: float) -> None:
    """Run the refresh loop; whatever goes wrong in it must not take the server down"""
    try:
        await run_refresh_loop(interval)
    except Exception:
        logger.exception("The Kroger token refresh stopped unexpectedly; the sign-in is now renewed only on use")


@lifespan
async def refresh_lifespan(server):
    """Keep the Kroger sign-in renewed for as long as the server runs"""
    interval = refresh_interval()
    if interval <= 0:
        yield {}
        return
    # A plain task, not a task group: the lifespan may be entered and left from different tasks
    task = asyncio.get_running_loop().create_task(_run_refresh_loop(interval), name="kroger-token-refresh")
    try:
        yield {}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_server() -> FastMCP:
    """Create and configure the FastMCP server instance"""
    # A .env file in the working directory supplements the environment
    load_env_file()

    # Initialize the FastMCP server
    mcp = FastMCP(
        name="Kroger API Server",
        lifespan=refresh_lifespan,
        instructions="""
        This MCP server provides access to Kroger's API for grocery shopping functionality.
        
        Key Features:
        - Search and manage store locations
        - Find and search products
        - Add items to shopping cart with local tracking
        - Access chain and department information
        - User profile management
        
        Common workflows:
        1. Set a preferred location with set_preferred_location
        2. Search for products with search_products
        3. Add items to cart with add_items_to_cart
        4. Use bulk_add_to_cart for multiple items at once. To find many items, use
           bulk_search_products: it returns a shortlist of up to 3 one-line options per term.
           When the options differ in brand, price or type, ask the user which they want before
           adding anything; items added to the cart cannot be removed from the real Kroger cart.
        5. View current cart with view_current_cart
        6. Mark order as placed with mark_order_placed
        
        Shopping rules:
        - Check the sign-in first. Call get_authentication_info before a shopping session: a dead
          sign-in found in the middle of a list wastes the work already done. When it says the user is
          not authenticated, or refresh.state is signin_needed, follow the Authentication Flow below.
        - Decide before adding. When the search results for an item differ in brand, price or type
          (organic, for example), ask the user which they want, with the prices. Never add a guess and
          swap it later: items added to the cart cannot be removed from the real Kroger cart through
          this server.
        - Quantities. An item sold by weight (produce, meat, deli) is quantity 1: Kroger weighs it when the
          order is fulfilled, and a packaged pound of meat is one item. A packaged item is one per unit. When the
          size is smaller than the amount needed (an 8 oz bag when 16 oz is needed), add enough units.
        - An empty search result does not mean the store does not stock the item. Try an alternate
          term: a more general or differently worded one, or the brand.
        - A product that is TEMPORARILY_OUT_OF_STOCK may be back later. Offer the next option, or
          search again later; stock levels change during the day.

        Limits:
        This server can only add items to the user's Kroger cart. It cannot check out, place an order, pay,
        or schedule fulfillment: the user does that in the Kroger app or website, and the agent should say so
        when the list is done. The Kroger API cannot read or change the real cart, so view_current_cart,
        remove_from_cart and clear_current_cart only read or edit this server's own record of what it added.
        They do not show what is in the real cart, and they do not remove anything from it. Never tell the
        user an item was removed from their cart; tell them it was removed from this record, and that they
        must remove it in the Kroger app.

        Authentication Flow:
        1. Use start_authentication to get an authorization URL
        2. Open the URL in your browser and authorize the application
        3. Copy the full redirect URL from your browser
        4. Use complete_authentication with the redirect URL to finish the process
        
        Cart Tracking:
        This server maintains a local record of items added to your cart since the Kroger API
        doesn't provide cart viewing functionality. When you place an order through the Kroger
        website/app, use mark_order_placed to move the current cart to order history.
        
        Errors:
        A tool that fails returns an error result. Its text says what went wrong and the next
        step to take. Its structured content holds an "error" object with a "type" (such as
        invalid_arguments, invalid_parameter, authentication_required, not_found, or
        rate_limited), a "next_step", and for Kroger API errors the HTTP status, Kroger's error
        code and message, and a correlation id. Follow the next step instead of retrying
        the same call unchanged.
        """
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness for a container healthcheck (HTTP transports only)

        Answers 200 while the process serves requests. It never calls Kroger, and it
        stays 200 when the sign-in needs attention: restarting the server cannot fix
        that, and would only lose a sign-in in progress. The sign-in state is in the
        body, and in get_authentication_info.
        """
        return JSONResponse({"status": "ok", "refresh": signin_status.as_dict()})

    # Return tool failures as error results with structured details
    mcp.add_middleware(ToolErrorMiddleware())

    # Register all tools from the modules
    location_tools.register_tools(mcp)
    product_tools.register_tools(mcp)
    cart_tools.register_tools(mcp)
    info_tools.register_tools(mcp)
    profile_tools.register_tools(mcp)
    utility_tools.register_tools(mcp)
    auth_tools.register_tools(mcp)
    
    # Register prompts
    prompts.register_prompts(mcp)
    
    return mcp


def main():
    """Run the server; the command-line options are in kroger_mcp.cli"""
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
