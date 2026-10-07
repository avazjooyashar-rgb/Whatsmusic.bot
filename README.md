# Music Recognition Telegram Bot

Send a voice message, a text, or a YouTube / Instagram / Spotify link and get an mp3 back.

Pipeline: voice -> ACRCloud -> AudD (fallback) -> Odesli -> YouTube -> yt-dlp -> mp3

## Setup
1. Install ffmpeg, then `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in, then `export $(cat .env | xargs)`
3. `python bot.py`

## Admin commands (only IDs in ADMIN_IDS)
- `/addacr HOST KEY SECRET` add an ACRCloud account
- `/addaudd TOKEN` add an AudD account
- `/accounts` list, `/toggle ID` enable/disable, `/remove ID` delete

## Notes
- Use your own paid/legitimate API accounts and respect each service's terms.
- Downloading copyrighted content may violate platform terms or local law. Use responsibly.
