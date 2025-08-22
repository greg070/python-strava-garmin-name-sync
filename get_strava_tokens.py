from stravalib.client import Client
import pickle
from dotenv import load_dotenv
import os

load_dotenv()

CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')

client = Client()

url = client.authorization_url(client_id=CLIENT_ID, redirect_uri='http://127.0.0.1:5000/authorization', scope=['read','activity:read_all','activity:write'])

print(f"Please visit this URL to authorize the application: {url}")

CODE = input("Enter the code you received after authorization: ")

token_response = client.exchange_code_for_token(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    code=CODE
)

print("Access Token:", token_response['access_token'])
print("Refresh Token:", token_response['refresh_token'])
print("Expires At:", token_response['expires_at'])

with open('access_token.pickle', 'wb') as f:
    pickle.dump(token_response, f)