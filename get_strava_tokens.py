#!/usr/bin/env python3
"""
Script to obtain Strava API tokens and save them where the sync app expects them
(data/.strava_token.json).
"""

import json
import os
from dotenv import load_dotenv
from stravalib.client import Client

load_dotenv()

CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
TOKEN_PATH = "data/.strava_token.json"

client = Client()

url = client.authorization_url(client_id=CLIENT_ID,
                               redirect_uri='http://127.0.0.1:5000/authorization',
                               scope=['read', 'activity:read_all', 'activity:write'])

print(f"Please visit this URL to authorize the application: {url}")

CODE = input("Enter the code you received after authorization: ")

token_response = client.exchange_code_for_token(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    code=CODE
)

os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
    json.dump({
        "access_token": token_response['access_token'],
        "refresh_token": token_response['refresh_token'],
        "expires_at": token_response['expires_at'],
    }, f, indent=2)

print(f"Tokens saved to {TOKEN_PATH} (expires_at={token_response['expires_at']}).")
print("The sync app will load and refresh them automatically from there.")
