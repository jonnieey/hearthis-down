#!/usr/bin/env python3
"""
Professional HearThis.at Downloader

Features:
- Download all tracks from an artist or a single track from a URL
- Async download with configurable concurrency
- Comprehensive error handling and logging
- Configurable download directory and formats
- Resumable downloads with progress tracking
- Duplicate file detection
- Rate limiting with exponential backoff
"""

import argparse
import asyncio
import aiohttp
import configparser
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import yt_dlp
from pyhearthis.hearthis import HearThis
from xdg_base_dirs import xdg_config_home, xdg_cache_home

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hearthis_down")

DEFAULT_CONFIG = {
    "email": "",
    "password": "",
    "audio_dir": "~/Music/hearthisat",
    "max_concurrent_downloads": "3",
    "download_timeout": "30",
    "max_retries": "3",
    "format": "bestaudio/best",
    "rate_limit": "1.0",  # seconds between requests
}


class ConfigManager:
    """Manage configuration settings"""

    def __init__(self):
        self.config_dir = Path(xdg_config_home()) / "hearthis_down"
        self.config_file = self.config_dir / "config"
        self.cache_dir = Path(xdg_cache_home()) / "hearthis_down"
        self.config = configparser.ConfigParser(defaults=DEFAULT_CONFIG)

        # Ensure directories exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load_config(self):
        """Load configuration from file"""
        if self.config_file.exists():
            self.config.read(self.config_file)
        else:
            self.create_default_config()
        return self.config["DEFAULT"]

    def create_default_config(self):
        """Create default configuration file"""
        self.config["DEFAULT"] = DEFAULT_CONFIG
        with open(self.config_file, "w") as configfile:
            self.config.write(configfile)
        logger.info(f"Created default config at {self.config_file}")


class HearThisDownloader:
    """Main downloader class"""

    def __init__(self, config):
        self.config = config
        self.audio_dir = Path(
            config.get("audio_dir", "~/Music/hearthisat")
        ).expanduser()
        self.max_concurrent = int(config.get("max_concurrent_downloads", "3"))
        self.download_timeout = int(config.get("download_timeout", "30"))
        self.max_retries = int(config.get("max_retries", "3"))
        self.format = config.get("format", "bestaudio/best")
        self.rate_limit = float(config.get("rate_limit", "1.0"))
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.downloaded_files = set()

    async def initialize(self):
        """Initialize the downloader"""
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        # Load already downloaded files
        self._load_downloaded_files()

    def _load_downloaded_files(self):
        """Load already downloaded files to avoid re-downloading"""
        for ext in [".mp3", ".m4a", ".flac", ".wav", ".ogg"]:
            for file in self.audio_dir.rglob(f"*{ext}"):
                self.downloaded_files.add(file.stem)

    def extract_info_from_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract artist and track information from a HearThis URL"""
        # Parse the URL
        parsed = urlparse(url)

        # Check if it's a hearthis.at URL
        if "hearthis.at" not in parsed.netloc:
            return None, None

        # Extract path components
        path_parts = parsed.path.strip("/").split("/")

        # Handle different URL formats:
        # 1. https://hearthis.at/artist/track/
        # 2. https://hearthis.at/artist/set/setname/
        if len(path_parts) >= 2:
            artist = path_parts[0]
            # If it's a set, we can't download individual tracks from the set URL
            if "set" in path_parts and len(path_parts) > 2:
                logger.warning(
                    "Set URLs are not supported for individual track downloads"
                )
                return artist, None
            else:
                # Assume the last part is the track name
                track = path_parts[-1]
                return artist, track

        return None, None

    async def get_user_tracks(self, artist: str) -> List[str]:
        """Get all tracks for an artist"""
        async with aiohttp.ClientSession() as session:
            try:
                hearthis = HearThis(session)
                user = await hearthis.login(
                    self.config.get("email"), self.config.get("password")
                )

                # Search for artist
                artist_search_result = await hearthis.get_single_artist(user, artist)
                if not artist_search_result:
                    logger.error(f"Could not find artist: {artist}")
                    return []

                # Get all tracks
                tracks = []
                page = 1
                count = 20  # Max allowed by API

                while True:
                    logger.info(f"Fetching page {page} of tracks for {artist}")
                    search_result = await hearthis.get_artist_tracks(
                        user, artist_search_result.permalink, page=page, count=count
                    )

                    if not search_result or len(search_result) == 0:
                        break

                    tracks.extend(search_result)
                    page += 1

                    # Rate limiting
                    await asyncio.sleep(self.rate_limit)

                logger.info(f"Found {len(tracks)} tracks for {artist}")
                return [
                    track.download_url
                    for track in tracks
                    if hasattr(track, "download_url")
                ]

            except Exception as e:
                logger.error(f"Error fetching tracks: {e}")
                return []

    async def download_track(self, session, url: str, retry_count: int = 0) -> bool:
        """Download a single track with retry logic"""
        if retry_count >= self.max_retries:
            logger.error(f"Max retries exceeded for {url}")
            return False

        async with self.semaphore:
            try:
                # Extract info to check if already downloaded
                with yt_dlp.YoutubeDL({"quiet": True, "simulate": True}) as ydl:
                    info = await asyncio.to_thread(
                        ydl.extract_info, url, download=False
                    )
                    if info.get("title") in self.downloaded_files:
                        logger.info(f"Skipping already downloaded: {info.get('title')}")
                        return True

                # Download the track
                ydl_opts = {
                    "format": self.format,
                    "outtmpl": str(self.audio_dir / "%(title)s.%(ext)s"),
                    "quiet": False,
                    "no_warnings": False,
                    "continuedl": True,
                    "ignoreerrors": False,
                    "retries": self.max_retries,
                    "fragment_retries": self.max_retries,
                    "timeout": self.download_timeout,
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                    },
                }

                logger.info(f"Downloading: {url}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    await asyncio.to_thread(ydl.download, [url])

                # Add to downloaded files
                with yt_dlp.YoutubeDL({"quiet": True, "simulate": True}) as ydl:
                    info = await asyncio.to_thread(
                        ydl.extract_info, url, download=False
                    )
                    self.downloaded_files.add(info.get("title"))

                return True

            except yt_dlp.utils.DownloadError as e:
                logger.warning(f"Download error (attempt {retry_count + 1}): {e}")
                await asyncio.sleep(2**retry_count)  # Exponential backoff
                return await self.download_track(session, url, retry_count + 1)

            except Exception as e:
                logger.error(f"Unexpected error downloading {url}: {e}")
                return False

    async def download_tracks_async(self, urls: List[str]):
        """Download all tracks concurrently with rate limiting"""
        if not urls:
            logger.warning("No tracks to download")
            return

        async with aiohttp.ClientSession() as session:
            tasks = [self.download_track(session, url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            successful = sum(1 for r in results if r is True)
            logger.info(f"Download completed: {successful}/{len(urls)} successful")


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Download tracks from HearThis.at")
    parser.add_argument("input", nargs="?", help="Artist name or track URL")
    parser.add_argument("--email", help="HearThis.at email")
    parser.add_argument("--password", help="HearThis.at password")
    parser.add_argument("--dir", help="Download directory")
    parser.add_argument("--concurrent", type=int, help="Max concurrent downloads")
    parser.add_argument("--format", help="Audio format preference")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    group.add_argument("--quiet", "-q", action="store_true", help="Quiet output")

    return parser.parse_args()


async def main():
    """Main function"""
    args = parse_args()

    if not args.input:
        print("Error: You must provide either an artist name or a track URL")
        print("Usage: hearthis_down [ARTIST|URL]")
        sys.exit(1)

    # Load configuration
    config_manager = ConfigManager()
    config = config_manager.load_config()

    # Override config with command line arguments
    if args.email:
        config["email"] = args.email
    if args.password:
        config["password"] = args.password
    if args.dir:
        config["audio_dir"] = args.dir
    if args.concurrent:
        config["max_concurrent_downloads"] = str(args.concurrent)
    if args.format:
        config["format"] = args.format

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.quiet:
        logging.getLogger().setLevel(logging.CRITICAL)

    # Validate credentials (only needed for artist downloads)
    downloader = HearThisDownloader(config)
    await downloader.initialize()

    # Check if input is a URL or artist name
    if args.input.startswith(("http://", "https://")):
        # Single URL download
        logger.info(f"Downloading track from URL: {args.input}")
        await downloader.download_tracks_async([args.input])
    else:
        # Artist download
        if not config.get("email") or not config.get("password"):
            logger.error(
                "Email and password required for artist downloads. Set in config or use --email and --password"
            )
            sys.exit(1)

        # Normalize artist name
        artist = args.input.lower().replace(" ", "-")

        # Get and download tracks
        logger.info(f"Fetching tracks for artist: {artist}")
        tracks = await downloader.get_user_tracks(artist)

        if tracks:
            logger.info(f"Starting download of {len(tracks)} tracks")
            await downloader.download_tracks_async(tracks)
        else:
            logger.warning("No tracks found to download")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Download interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
