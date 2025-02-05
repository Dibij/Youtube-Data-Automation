import os
import re
import json
import logging
from typing import Optional, Dict, List, Tuple, Any
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Set up logging
log_folder = 'data/logs'
os.makedirs(log_folder, exist_ok=True)  # Create 'logs' folder inside 'data' if it doesn't exist

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_folder, 'youtube_api.log')),  # Log to data/logs folder
        logging.StreamHandler()
    ]
)

class YouTubeAnalyzer:
    def __init__(self):
        # Load environment variables
        load_dotenv()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        if not self.api_key:
            raise ValueError("YouTube API key not found in environment variables")
        self.youtube = self._initialize_api()

    def _initialize_api(self):
        """Initialize YouTube API client"""
        try:
            return build('youtube', 'v3', developerKey=self.api_key)
        except Exception as e:
            logging.error(f"Failed to initialize YouTube API: {str(e)}")
            raise

    def get_channel_statistics(self, channel_id: str) -> Dict[str, Any]:
        """Get channel statistics including subscribers and views"""
        try:
            response = self.youtube.channels().list(
                part="statistics",
                id=channel_id
            ).execute()

            if not response.get('items'):
                logging.warning(f"No statistics found for channel {channel_id}")
                return {'subscriberCount': 0, 'viewCount': 0, 'videoCount': 0}

            stats = response['items'][0]['statistics']
            return {
                'subscriberCount': int(stats.get('subscriberCount', 0)),
                'viewCount': int(stats.get('viewCount', 0)),
                'videoCount': int(stats.get('videoCount', 0))
            }
        except HttpError as e:
            logging.error(f"HTTP error getting statistics for {channel_id}: {str(e)}")
            return {'subscriberCount': 0, 'viewCount': 0, 'videoCount': 0}
        except Exception as e:
            logging.error(f"Error getting statistics for {channel_id}: {str(e)}")
            return {'subscriberCount': 0, 'viewCount': 0, 'videoCount': 0}

    def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        """Get comprehensive channel information"""
        try:
            response = self.youtube.channels().list(
                part="snippet,contentDetails,statistics",
                id=channel_id
            ).execute()

            if not response.get('items'):
                logging.warning(f"No information found for channel {channel_id}")
                return {}

            channel_data = response['items'][0]
            snippet = channel_data['snippet']
            statistics = channel_data['statistics']

            # Only process channels with sufficient subscribers
            subscriber_count = int(statistics.get('subscriberCount', 0))
            if subscriber_count > 50000:
                return {}

            return {
                'title': snippet.get('title'),
                'description': snippet.get('description', ''),
                'country': snippet.get('country', 'N/A'),
                'publishedAt': snippet.get('publishedAt'),
                'customUrl': snippet.get('customUrl', f"https://youtube.com/channel/{channel_id}"),
                'statistics': statistics
            }
        except HttpError as e:
            logging.error(f"HTTP error getting channel info for {channel_id}: {str(e)}")
            return {}
        except Exception as e:
            logging.error(f"Error getting channel info for {channel_id}: {str(e)}")
            return {}

    def extract_contact_info(self, description: str) -> Dict[str, str]:
        """Extract email and website from channel description"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
        website_pattern = r'https?://(?:www\.)?(?!youtube\.com|youtu\.be)[^\s<>"]+'

        email = re.search(email_pattern, description)
        website = re.search(website_pattern, description)

        return {
            'email': email.group(0) if email else 'N/A',
            'website': website.group(0) if website else 'N/A'
        }

    def get_channels_from_country(self, country_code: str) -> List[Dict[str, Any]]:
        """Get channels from a specific country with more than 5000 subscribers, limited to 50 channels"""
        channels = []
        seen_channels = set()
        try:
            while len(channels) < 50:
                response = self.youtube.search().list(
                    part="snippet",
                    type="channel",
                    regionCode=country_code,
                    maxResults=50,
                    order="viewCount"
                ).execute()

                for item in response.get('items', []):
                    channel_id = item['snippet']['channelId']
                    if channel_id in seen_channels:
                        continue
                    seen_channels.add(channel_id)

                    channel_info = self.get_channel_info(channel_id)
                    if channel_info:
                        contact_info = self.extract_contact_info(channel_info.get('description', ''))
                        statistics = channel_info.get('statistics', {})

                        channels.append({
                            'channelId': channel_id,
                            'name': channel_info.get('title', 'N/A'),
                            'url': channel_info.get('customUrl'),
                            'country': channel_info.get('country', 'N/A'),
                            'createDate': channel_info.get('publishedAt', 'N/A'),
                            'subscribers': int(statistics.get('subscriberCount', 0)),
                            'totalViews': int(statistics.get('viewCount', 0)),
                            'totalVideos': int(statistics.get('videoCount', 0)),
                            'email': contact_info['email'],
                            'website': contact_info['website']
                        })

                    if len(channels) >= 50:
                        break

            return channels
        except Exception as e:
            logging.error(f"Error getting channels from {country_code}: {str(e)}")
            return []

    def save_to_json(self, data: List[Dict[str, Any]], filename: str = "channel_data.json") -> None:
        """Save channel data to a JSON file"""
        try:
            # Create the 'channel_info' folder inside 'data' if it doesn't exist
            json_folder = 'data/channel_info'
            os.makedirs(json_folder, exist_ok=True)

            # Save the JSON file to the 'channel_info' folder
            file_path = os.path.join(json_folder, filename)
            with open(file_path, "w", encoding="utf-8") as json_file:
                json.dump(data, json_file, ensure_ascii=False, indent=4)
            logging.info(f"Successfully saved data to {file_path}")
        except Exception as e:
            logging.error(f"Error saving data to {filename}: {str(e)}")
            raise

def main():
    try:
        analyzer = YouTubeAnalyzer()
        country_code = 'US'  # Dynamically set the country code here (e.g., 'US' for the United States)
        logging.info(f"Starting channel analysis for country: {country_code}")
        channels = analyzer.get_channels_from_country(country_code)  # Pass the country_code dynamically
        analyzer.save_to_json(channels)
    except Exception as e:
        logging.error(f"Script execution failed: {str(e)}")
        raise

if __name__ == "__main__":
    main()
