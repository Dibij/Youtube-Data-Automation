# YouTube API Analyzer

## Overview
This project is a YouTube API Analyzer that retrieves information about YouTube channels from a specific country using the YouTube Data API v3. It fetches details such as channel statistics, descriptions, and contact information (emails & websites) and saves the data into a JSON file.

## Features
- Fetches top YouTube channels by view count from a given country.
- Retrieves detailed statistics, including subscriber count, total views, and video count.
- Extracts emails and website links from channel descriptions.
- Saves the extracted data into a JSON file.
- Logs execution details and errors for easy debugging.

## Prerequisites
Before running this project, ensure you have the following:
- Python 3 installed
- A YouTube Data API v3 key
- Required Python packages installed

## Installation
1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd <repository-folder>
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up your API key:
   - Create a `.env` file in the root directory.
   - Add the following line:
     ```env
     YOUTUBE_API_KEY=your_api_key_here
     ```

## Usage
Run the script using the following command:
```bash
python src/main.py
```
This will fetch the top YouTube channels from Nepal (default country code: NP) and save the data to `channel_data.json`.

## File Structure
```
📂 Project Root
├── 📂 src
│   ├── main.py  # Main script to fetch and analyze YouTube data
├── requirements.txt  # List of dependencies
├── README.md  # Project documentation
```

## API Quota Considerations
- YouTube Data API has a quota limit. If you exceed it, you will receive a `quotaExceeded` error.
- To monitor your API quota, check your [Google Cloud Console](https://console.cloud.google.com/apis/dashboard).

## Future Enhancements
- Implement pagination to fetch more channels beyond the 50-channel limit.
- Store data in a database instead of a JSON file.
- Add support for fetching additional video analytics.

## License
This project is licensed under the MIT License.

