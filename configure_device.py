"""Copy only the local server URL/device token into the ignored firmware header."""
import argparse
import ipaddress
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'firmware'))
from prepare import prepare, validate_config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-url', help='LAN address matching PUBLIC_BASE_URL in .env')
    args=parser.parse_args()
    from server import settings
    cfg=settings()
    url=validate_config(args.server_url or cfg['public_base_url'],cfg['device_token'])
    host=urlsplit(url).hostname
    try:
        loopback=ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback=host=='localhost'
    if loopback:
        parser.error('PUBLIC_BASE_URL is loopback. Set the Windows LAN IP in .env before configuring ESP.')
    if args.server_url and url != cfg['public_base_url']:
        parser.error('Set the same PUBLIC_BASE_URL in .env first. Mismatched origins prevent reminder audio playback.')
    prepare(url,cfg['device_token'])
    print('Firmware server: '+url)
    print('Device token copied privately; Gemini API key is NEVER put in firmware.')
    print('Now open firmware in PlatformIO and Build/Upload. Restart server after changing .env.')

if __name__=='__main__': main()
