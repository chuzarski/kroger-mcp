"""Tool annotations: what a tool does to state, so clients can tell the cart tools apart

Every tool picks one of the three, passed as @mcp.tool(annotations=...).
"""

# Looks something up; changes nothing
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}

# Adds or changes state (the cart, preferences, a sign-in) without deleting anything
WRITE = {"readOnlyHint": False, "destructiveHint": False}

# Deletes state: cart records or the saved sign-in
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}
