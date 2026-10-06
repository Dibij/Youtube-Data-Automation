from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# THIS IS THE FIXED LINE. Make sure it matches exactly!
SCOPES = ["https://googleapis.com"]
CLIENT_SECRETS_FILE = "client_secret.json"


def get_authenticated_service():
  flow = InstalledAppFlow.from_client_secrets_file(
      CLIENT_SECRETS_FILE, SCOPES
  )
  credentials = flow.run_local_server(port=56383, prompt="select_account")
  return build("youtube", "v3", credentials=credentials)


youtube = get_authenticated_service()
request = youtube.playlists().list(part="snippet,status", mine=True)
response = request.execute()

for playlist in response.get("items", []):
  print(playlist["snippet"]["title"], playlist["status"]["privacyStatus"])
