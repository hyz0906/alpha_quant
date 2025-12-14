import pymupdf4llm
from config.logging_config import setup_logging
import os

logger = setup_logging()

class ReportConverter:
    def __init__(self):
        pass

    def convert_to_markdown(self, pdf_path: str) -> str:
        """
        Convert PDF file to Markdown using pymupdf4llm.
        """
        if not os.path.exists(pdf_path):
            logger.error(f"File not found: {pdf_path}")
            return ""

        try:
            logger.info(f"Converting {pdf_path} to Markdown...")
            md_text = pymupdf4llm.to_markdown(pdf_path)
            return md_text
        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            return ""
