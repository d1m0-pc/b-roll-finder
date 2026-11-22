# B-roll-finder

Find B-roll clips for your videos in seconds. Type a few keywords (or paste a podcast transcript),
and B-roll-finder searches stock sites and YouTube, shows previews, and lets you download everything
you pick in one go.

It runs locally on your computer in your browser. No account, no server, no hosting.

## Features

- **Multi-source search**: Pexels, Pixabay, Coverr and YouTube in one grid, with hover previews.
- **Several phrases at once**: `cyber security, hacker typing, server room` gives a separate section per phrase.
- **Transcript mode**: paste a transcript, Claude suggests search phrases for each part, and the app finds clips automatically.
- **Batch download** to a folder or as a ZIP, with optional numbering (`01_`, `02_`, ...) in selection order.
- **Filters**: orientation, duration, minimum quality, sort by duration.
- **Vertical 9:16 export**: crops clips to 1080×1920 for Shorts, Reels and TikTok (needs ffmpeg).
- **Favorites** (star), a "downloaded" badge, and search history.
- **Paste links** from Pinterest, TikTok, Instagram, YouTube or Vimeo (one or many) and download them together via yt-dlp.
- Each search section has "Open on" links for Pinterest, TikTok, Instagram and YouTube, which open their search page in a new tab.

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) and tick **Add Python to PATH**.
2. Optional, for 9:16 and YouTube trims: install ffmpeg (`winget install ffmpeg` in PowerShell), then reopen the terminal.
3. Copy `keys.example.txt` to `keys.txt` and add the API keys you have (none are required, one source is enough).
4. Double-click `run.bat`. Your browser opens at http://127.0.0.1:5055

### macOS / Linux

    pip install -r requirements.txt
    cp keys.example.txt keys.txt
    python app.py

## API keys

Put them in `keys.txt` as `NAME=value`, with no spaces and no quotes. **Never commit `keys.txt`** (it is in `.gitignore`).

| Key | Where to get it | Notes |
|---|---|---|
| `PIXABAY_KEY` | pixabay.com/api/docs | Shown on the page when you are logged in |
| `PEXELS_KEY` | pexels.com/api | Pexels may pause new keys |
| `COVERR_KEY` | email team@coverr.co | Check their API terms before commercial use |
| `YOUTUBE_KEY` | Google Cloud Console, enable "YouTube Data API v3", create an API key | Free quota is about 100 searches per day |
| `ANTHROPIC_KEY` | console.anthropic.com, API keys | Only for transcript mode; pay-as-you-go, a transcript costs cents |
| `CLAUDE_MODEL` | optional | Defaults to `claude-haiku-5-5` |

## Notes

- **Rights.** Stock sites have their own licenses (check each one). Clips from YouTube, TikTok, Instagram and Pinterest
  usually belong to someone else. You are responsible for having the right to use anything you download.
- **YouTube clips** are trimmed instead of downloaded whole: enter from/to seconds on the card (empty means the first 60 s).
  Needs ffmpeg and yt-dlp (`run.bat` installs and updates yt-dlp).
- **Pasted links without from/to** download the whole clip, up to 10 minutes.
- Instagram and TikTok sometimes require a login, so a download can fail. Other clips in the same batch still download.
- Mixkit and Videvo have no public API, so they are not included.
- The app only listens on `127.0.0.1` and rejects requests from other sites.
- Search history and favorites are stored in `data.json` next to the app.
