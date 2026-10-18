from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from google_auth_oauthlib.flow import Flow

from planner.services.calendar_sync import SCOPES


def oauth_flow(state=None, code_verifier=None):
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise ImproperlyConfigured(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in the local .env file."
        )
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1:8000/google/callback/"],
        }
    }
    flow_options = {}
    if code_verifier is not None:
        flow_options = {
            "code_verifier": code_verifier,
            "autogenerate_code_verifier": False,
        }
    flow = Flow.from_client_config(client_config, scopes=SCOPES, state=state, **flow_options)
    flow.redirect_uri = "http://127.0.0.1:8000/google/callback/"
    return flow
