"""Use LiteLLM's stock device flow; never print the resulting access token."""
import os
from litellm.llms.chatgpt.authenticator import Authenticator

os.umask(0o077)
Authenticator().get_access_token()
print("LiteLLM ChatGPT login complete.", flush=True)
