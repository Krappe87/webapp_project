import os
import msal
import httpx

# Load credentials from environment
TENANT_ID = os.getenv("MS_TENANT_ID")
CLIENT_ID = os.getenv("MS_CLIENT_ID")
CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
SITE_ID = os.getenv("SHAREPOINT_SITE_ID")
ITEM_ID = os.getenv("EXCEL_ITEM_ID")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

async def get_graph_token():
    """Silently authenticates with Microsoft Entra ID to get a Graph API token."""
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        print("SharePoint credentials missing. Skipping Graph API call.")
        return None

    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )
    
    # First, try to get a token from the local memory cache
    result = app.acquire_token_silent(SCOPES, account=None)
    
    if not result:
        # If no valid token in cache, negotiate a new one with Microsoft
        result = app.acquire_token_for_client(scopes=SCOPES)

    if "access_token" in result:
        return result["access_token"]
    else:
        print(f"Failed to acquire MS Graph token: {result.get('error_description')}")
        return None

async def update_client_alert_count(client_id: str, week_label: str, total_alerts: int):
    """
    Locates the client's row and the current week's column in the Excel file,
    then updates the cell with the new total.
    """
    token = await get_graph_token()
    if not token:
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # The Graph API URL format to target a specific workbook
    # https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}/workbook/worksheets/{sheet_name}/...
    
    # TODO: We will build the exact JSON payload and cell mapping logic here
    # Get: Tab names, Header Row, Anchor - The Notes column. 
    # once we define exactly how your spreadsheet is structured!
    
    print(f"Graph API ready. Preparing to update {client_id} for {week_label} with {total_alerts} alerts.")
    return True