from loguru import logger
import requests
import os

class ReportCrawler:
    def __init__(self, download_dir="data/reports"):
        self.download_dir = download_dir
        os.makedirs(self.download_dir, exist_ok=True)

    def fetch_report(self, url: str, filename: str) -> str:
        """
        Fetch PDF from URL and save to local.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        try:
            logger.info(f"Downloading {url}...")
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                path = os.path.join(self.download_dir, filename)
                with open(path, "wb") as f:
                    f.write(response.content)
                logger.success(f"Saved report to {path}")
                return path
            else:
                logger.error(f"Failed to download {url}: Status {response.status_code}")
                return ""
        except Exception as e:
            logger.error(f"Download error: {e}")
            return ""
