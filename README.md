# Strava-Garmin Name Synchronizer

Python application that automatically syncs your Strava activity names and descriptions with Garmin Connect data.

Did you love the Strava feature enabled in June that synced your Garmin activity names and descriptions automatically?  
Strava has since removed this feature, but **you can get it back** with this project!  
This tool restores automatic synchronization of your Garmin activity names and descriptions to Strava, just like before.


## Features

- ✅ Automatic activity name synchronization
- ✅ Activity description synchronization
- ✅ API rate limiting compliance
- ✅ Automatic Strava token refresh
- ✅ Detailed logging
- ✅ Ready-to-use Docker image
- ✅ Configurable scheduler

## Prerequisites

### Strava Setup

1. Create a Strava app at [strava setting API](https://www.strava.com/settings/api/)
2. Get your `Client ID` and `Client Secret`
3. Generate an initial access token via the OAuth2 process 

### Garmin Setup

- Garmin Connect account with email and password

## Installation & Configuration

### 1. Clone the repository

```bash
git clone <repository-url>
cd strava-garmin-sync
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit the `.env` file with your real values:

```bash
# Strava Configuration
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=your_secret_here
STRAVA_ACCESS_TOKEN=your_token_here
STRAVA_REFRESH_TOKEN=your_refresh_token_here
STRAVA_TOKEN_EXPIRES_AT=your_token_expiry_timestamp

# Garmin Configuration
GARMIN_EMAIL=your_email@example.com
GARMIN_PASSWORD=your_password

# General Configuration
SYNC_INTERVAL_MINUTES=60
RUN_MODE=scheduler
```

## One-Time Strava Authorization Required

Before using this project, you must run the `get_strava_tokens.py` script **once** to authorize your Strava account and obtain your access and refresh tokens.

**Steps:**
1. Make sure your `.env` file is filled with your Strava client credentials (see `.env.example`).
2. Run the script:
   ```bash
   python get_strava_tokens.py
   ```
3. Follow the instructions in the terminal to authorize the app in your browser and paste the code you receive.
4. The script will save your tokens for use by the main synchronization tool.

> **Important Note:**  
> When you run `get_strava_tokens.py`, you will be given a URL to open in your browser.  
> Open this URL and log in to Strava to give consent to the application.  
> After granting consent, Strava will redirect you to the `REDIRECT_URI` you specified (often `localhost`).  
> This page may not load (or show an error), but **the URL in your browser will contain an authorization code** as a parameter.  
> **Copy this code from the URL** and paste it back into the script when prompted.  
> This manual step is required only once per Strava account.

**You only need to do this once per Strava account (unless you revoke or change your app credentials).**

### 3. Deploy with Docker

#### Option A: Docker Compose (recommended)

```bash
docker-compose up -d
```

#### Option B: Classic Docker

```bash
# Build the image
docker build -t strava-garmin-sync .

# Run the container
docker run -d \
    --name strava-garmin-sync \
    --env-file .env \
    -v $(pwd)/logs:/app/logs \
    -v $(pwd)/data:/app/data \
    --restart unless-stopped \
    strava-garmin-sync
```

### 4. Verification

```bash
# Check logs
docker-compose logs -f strava-garmin-sync

# or
docker logs -f strava-garmin-sync
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `STRAVA_CLIENT_ID` | Your Strava app client ID | Required |
| `STRAVA_CLIENT_SECRET` | Your Strava app client secret | Required |
| `STRAVA_ACCESS_TOKEN` | Strava access token | Required |
| `STRAVA_REFRESH_TOKEN` | Strava refresh token | Required |
| `STRAVA_TOKEN_EXPIRES_AT` | Strava token expiry timestamp (epoch seconds) | Required |
| `GARMIN_EMAIL` | Garmin Connect email | Required |
| `GARMIN_PASSWORD` | Garmin Connect password | Required |
| `SYNC_INTERVAL_MINUTES` | Sync interval (minutes) | 60 |
| `RUN_MODE` | Run mode (`scheduler` or `once`) | scheduler |

### Run Modes

- **scheduler**: Continuous sync at the configured interval
- **once**: Single execution then stop

## Rate Limiting

The app automatically respects API limits:

- **Strava**: 100 requests per 15 minutes, 1000 per day
- **Garmin**: Appropriate delays between requests

## Getting Strava Tokens

### Simple method with curl

1. Authorize your app:
```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:write
```

2. Get the authorization code from the redirect URL

3. Exchange the code for a token:
```bash
curl -X POST https://www.strava.com/oauth/token \
    -F client_id=YOUR_CLIENT_ID \
    -F client_secret=YOUR_CLIENT_SECRET \
    -F code=YOUR_AUTHORIZATION_CODE \
    -F grant_type=authorization_code
```

## Monitoring & Logs

### Logs

Logs are stored in the `strava_garmin_sync.log` file and in the container's `./logs/` directory.

### Monitoring

```bash
# Monitor logs in real time
docker-compose logs -f strava-garmin-sync

# Container stats
docker stats strava-garmin-sync

# Container health
docker inspect strava-garmin-sync | grep Health -A 10
```

## Troubleshooting

### Common Errors

1. **Expired token**: The app automatically refreshes Strava tokens
2. **Rate limit reached**: The app waits automatically before resuming
3. **Garmin connection issue**: Check your credentials

### Debug

For more details, add:
```yaml
environment:
    - PYTHONPATH=/app
    - LOG_LEVEL=DEBUG
```

## Security

- Passwords are not hardcoded
- Use a `.env` file or Docker secrets
- The app runs as a non-root user
- Limit container resources

## Contributing

1. Fork the project
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

## Support

If you have issues:
1. Check the logs
2. Consult Strava and Garmin API documentation
3. Open an issue with details

## License

MIT License - see the LICENSE file for details.