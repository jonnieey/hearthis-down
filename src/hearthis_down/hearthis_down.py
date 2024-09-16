
"""
requires you to install 
yt-dlp; pip install yt-dl
pyhearthis ; pip install git+https://github.com/universalappfactory/pyhearthis
aiohttp; pip install aiohttp
"""

import asyncio
import aiohttp
from pyhearthis.hearthis import HearThis
import yt_dlp
import os
import sys
from pathlib import PosixPath, Path
import configparser
from xdg_base_dirs import xdg_config_home

AUDIO_DIR="~/Music/hearthisat"
CONFIG_FILE = Path(xdg_config_home() / 'hearthis_down' / 'config')

def usage():
    print(f"usage: {sys.argv[0]} <artist>")
    sys.exit(1)

if len(sys.argv) < 2:
    usage()

def create_config():
    config_parser = configparser.ConfigParser()
    config_parser['DEFAULT'] = {'email': '', 'password': '', 'audio_dir': AUDIO_DIR}
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as configfile:
        config_parser.write(configfile)

def read_config():
    if not CONFIG_FILE.exists():
        create_config()
    config_parser = configparser.ConfigParser()
    config_parser.read(CONFIG_FILE)
    return config_parser

config = read_config()['DEFAULT']

artist = sys.argv[1].lower().replace(" ", "-")
audio_dir = Path(config.get('audio_dir', AUDIO_DIR)).expanduser() / artist


async def get_user_tracks():

    async with aiohttp.ClientSession() as session:
        try:
            hearthis = HearThis(session)
            user = await hearthis.login(config.get('email'), config.get('password'))

            artist_search_result = await hearthis.get_single_artist(user, artist)
            if not artist_search_result:
                print(f"Could not find artist: {artist}")
                sys.exit(1)

            user_track_count = artist_search_result.track_count
            count = 15
            page = 1
            pages = [count] * (user_track_count//count) + ([user_track_count %
                                                            count] if user_track_count % count > 0 else [])
            links = []
            for page, count in enumerate(pages, start=1):
                search_result = await hearthis.get_artist_tracks(user, artist_search_result.permalink, page=page, count=count)
                if not search_result:
                    search_result = await hearthis.search(user, artist, page=page, count=count)
                links.extend(search_result)
            return [track.download_url for track in links]
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


async def download_track(track):
    ydl_opts = {
        'format': 'bestaudio/best',
        'continuedl': True,
        'ignoreerrors': True,
        'quiet': True,
        'paths': {
            'home': str(audio_dir)
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            await asyncio.to_thread(ydl.download, [track])
        except yt_dlp.utils.DownloadError as e:
            print(f"Failed to download {track}: {e}")
        except Exception as e:
                print(f"An unexpected error occurred for {track}: {e}")

async def download_tracks_async(tracks):
    tasks = [download_track(track) for track in tracks]
    await asyncio.gather(*tasks)


async def main():
    audio_dir.mkdir(parents=True, exist_ok=True)
    tracks = await get_user_tracks()
    await download_tracks_async(tracks) 

def run():
    asyncio.run(main())

if __name__ == "__main__":
    run()
