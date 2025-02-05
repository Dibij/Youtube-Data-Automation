Here’s an improved version of your `README.md` that organizes the sections better, provides clear instructions, and addresses potential issues like file structure and usage.

```markdown
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
- Python 3 installed.
- A YouTube Data API v3 key.
- Required Python packages installed.

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
   - Create a `.env` file in the root directory of the project.
   - Add the following line to the `.env` file:
     ```env
     YOUTUBE_API_KEY=your_api_key_here
     ```

## Usage
1. Run the script using the following command:
   ```bash
   python src/main.py
   ```

   By default, the script will fetch top YouTube channels from Nepal (country code: `NP`). You can modify the country code in the `main.py` file or make it dynamic as needed.

2. After execution, the retrieved channel data will be saved in a `channel_data.json` file in the `data/channel_info` folder. Logs will be saved in `data/logs/youtube_api.log`.

## File Structure
```
📂 Project Root
├── 📂 src
│   ├── main.py  # Main script to fetch and analyze YouTube data
│
├── 📂 data
│   ├── channel_info
│   │   └── channel_data.json  # JSON file containing the retrieved channel data
│   └── logs
│       └── youtube_api.log  # Log file containing execution details and errors
│
├── requirements.txt  # List of dependencies
├── .env  # File to store your YouTube API key
├── README.md  # Project documentation
```

## API Quota Considerations
- The YouTube Data API has a quota limit. If you exceed this limit, you will receive a `quotaExceeded` error.
- To monitor your API quota usage, check the [Google Cloud Console](https://console.cloud.google.com/apis/dashboard).

## Future Enhancements
- Implement pagination to fetch more than 50 channels beyond the current limit.
- Store data in a database instead of a JSON file for better scalability.
- Add support for fetching additional video analytics such as views, likes, and comments.
