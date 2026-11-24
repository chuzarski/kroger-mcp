"""
User profile and authentication tools for Kroger MCP server
"""

from typing import Dict, Any
from fastmcp import Context

from kroger_api.token_storage import USER_TOKEN_FILE, load_token

from .annotations import DESTRUCTIVE, READ_ONLY
from .signin_status import SIGNIN_NEEDED, status as signin_status
from ..errors import KrogerToolError, SIGN_IN_STEP, translate_errors
from .shared import get_authenticated_client, log_to_client, response_data, sign_out


def register_tools(mcp):
    """Register profile-related tools with the FastMCP server"""
    
    @mcp.tool(annotations=READ_ONLY)
    @translate_errors
    def get_user_profile(ctx: Context = None) -> Dict[str, Any]:
        """
        Get the authenticated user's Kroger profile information.

        The Identity API needs a sign-in that grants the profile.compact scope.
        
        Returns:
            Dictionary containing user profile data
        """
        log_to_client(ctx, "Getting user profile information")
        
        client = get_authenticated_client()
        profile = response_data(client.identity.get_profile(), dict, "Identity", "profile data")
        profile_id = profile.get("id", "N/A")
        
        log_to_client(ctx, f"Retrieved profile for user ID: {profile_id}")
        
        return {
            "success": True,
            "profile_id": profile_id,
            "message": "User profile retrieved successfully",
            "note": "The Kroger Identity API only provides the profile ID for privacy reasons."
        }

    @mcp.tool(annotations=READ_ONLY)
    @translate_errors
    def test_authentication(ctx: Context = None) -> Dict[str, Any]:
        """
        Check whether the user is signed in and the saved token has not expired.

        An expired token is refreshed if possible. The check compares the
        token's expiry time with the clock, so a sign-in that Kroger revoked
        early still reports token_valid true until a cart call fails. A user
        who is not signed in is a normal result (token_valid false), not an error.
        
        Returns:
            Dictionary indicating authentication status
        """
        log_to_client(ctx, "Testing authentication token validity")
        
        try:
            client = get_authenticated_client()
        except KrogerToolError as e:
            if e.error_type != "authentication_required":
                raise
            return {
                "success": True,
                "token_valid": False,
                "has_refresh_token": False,
                "can_auto_refresh": False,
                "message": f"{e.message} {SIGN_IN_STEP}"
            }
            
        result = {
            "success": True,
            "token_valid": True,
            "message": "Authentication token is valid"
        }
        
        # Check for refresh token availability
        has_refresh_token = "refresh_token" in client.client.token_info
        result["has_refresh_token"] = has_refresh_token
        result["can_auto_refresh"] = has_refresh_token
        
        if has_refresh_token:
            result["message"] += ". Token can be automatically refreshed when it expires."
        else:
            result["message"] += ". No refresh token available - will need to re-authenticate when token expires."
        
        return result

    @mcp.tool(annotations=READ_ONLY)
    @translate_errors
    def get_authentication_info(ctx: Context = None) -> Dict[str, Any]:
        """
        Get information about the current authentication state and token.

        A user who is not signed in is a normal result (authenticated false),
        not an error. So is a sign-in that Kroger no longer accepts: the server
        renews the sign-in in the background, and when Kroger rejects the
        refresh token the user has to sign in again.

        Returns:
            Dictionary containing authentication information, and "refresh":
            the state of the background renewal (ok, retrying, signin_needed,
            signed_out or unknown), when it last worked, and why it needs
            attention
        """
        log_to_client(ctx, "Getting authentication information")

        # A sign-in made by another process that shares the token file clears signin_needed
        signin_status.note_saved_token(load_token(USER_TOKEN_FILE))
        refresh = signin_status.as_dict()
        if refresh["state"] == SIGNIN_NEEDED:
            # The access token may still work for a while, but nothing can renew it
            return {
                "success": True,
                "authenticated": False,
                "message": f"Kroger no longer accepts the saved sign-in ({refresh['reason']}). {SIGN_IN_STEP}",
                "refresh": refresh,
            }

        try:
            client = get_authenticated_client()
        except KrogerToolError as e:
            if e.error_type != "authentication_required":
                raise
            return {
                "success": True,
                "authenticated": False,
                "message": f"{e.message} {SIGN_IN_STEP}",
                "refresh": signin_status.as_dict(),
            }
            
        result = {
            "success": True,
            "authenticated": True,
            "message": "User is authenticated",
            "refresh": signin_status.as_dict(),
        }
        
        token_info = client.client.token_info
        result.update({
            "token_type": token_info.get("token_type", "Unknown"),
            "has_refresh_token": "refresh_token" in token_info,
            "expires_in": token_info.get("expires_in"),
            "scope": token_info.get("scope", "Unknown")
        })
        
        # Don't expose the actual tokens for security
        result["access_token_preview"] = f"{token_info.get('access_token', '')[:10]}..." if token_info.get('access_token') else "N/A"
        
        if "refresh_token" in token_info:
            result["refresh_token_preview"] = f"{token_info['refresh_token'][:10]}..."
        
        # Get token file information if available
        if client.client.token_file:
            result["token_file"] = client.client.token_file
        
        return result

    @mcp.tool(annotations=DESTRUCTIVE)
    @translate_errors
    def force_reauthenticate(ctx: Context = None) -> Dict[str, Any]:
        """
        Force re-authentication by clearing the current authentication token.
        Use this if you're having authentication issues or need to log in as a different user.

        This deletes the saved user token. The local cart tracking and order
        history are kept.
        
        Returns:
            Dictionary indicating the re-authentication was initiated
        """
        log_to_client(ctx, "Forcing re-authentication by clearing current token")
        
        try:
            sign_out()
        except OSError as e:
            raise KrogerToolError(
                "local_state_error",
                f"Could not delete the saved user token: {e}",
                next_step="Delete the token file by hand, or check the permissions of its directory.",
            ) from e
        
        log_to_client(ctx, "Authentication token cleared")
        
        return {
            "success": True,
            "message": "The saved Kroger sign-in was deleted.",
            "next_step": SIGN_IN_STEP
        }
