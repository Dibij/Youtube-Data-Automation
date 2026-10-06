import os
import subprocess

# --- CONFIGURATION ---
PLAYLIST_URL = "https://youtube.com"
OPERA_COOKIE_PATH = r"C:\Users\dibij\AppData\Roaming\Opera Software\Opera GX Stable"
PHONE_MUSIC_DIR = "/sdcard/Music/"

def run_command(command, description):
    print(f"\n🚀 Running: {description}...")
    process = subprocess.run(command, shell=True, text=True)
    if process.returncode != 0:
        print(f"❌ Error during: {description}")
        return False
    return True

def main():
    # 1. Download and convert directly to high-quality MP3 locally using Deno/Node engine
    # This completely skips the video wrapper step entirely!
    ytdl_cmd = (
        f'yt-dlp --cookies-from-browser "opera:{OPERA_COOKIE_PATH}" '
        f'-x --audio-format mp3 --audio-quality 0 '
        f'--extractor-args "youtube:player_client=web_embedded" '
        f'"{PLAYLIST_URL}"'
    )
    
    if not run_command(ytdl_cmd, "Downloading and converting playlist to MP3"):
        return

    # 2. Sync files over to the phone via ADB
    adb_push_cmd = f'adb push *.mp3 {PHONE_MUSIC_DIR}'
    if not run_command(adb_push_cmd, "Pushing MP3 files to phone via ADB"):
        return

    # 3. Force Android indexer scan
    scan_cmd = 'adb shell content call --uri content://media/external/file --method scan_volume --arg external'
    if run_command(scan_cmd, "Refreshing phone media storage library"):
        print("\n🎉 All steps completed successfully! Enjoy your music.")

if __name__ == "__main__":
    main()
