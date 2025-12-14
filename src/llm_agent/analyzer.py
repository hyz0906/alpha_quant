import json
import os
from openai import OpenAI
from config.settings import settings
from config.logging_config import setup_logging

logger = setup_logging()

class ResearchAgent:
    def __init__(self, api_key: str = None, base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key or settings.DEEPSEEK_API_KEY
        if not self.api_key:
            logger.warning("DeepSeek API Key not found. LLM features will be disabled or fail.")
        
        self.client = OpenAI(
            api_key=self.api_key or "sk-dummy",
            base_url=base_url
        )

    def analyze_report(self, text: str) -> dict:
        """
        Analyze financial report text utilizing Design.md [P3-02] spec.
        """
        system_prompt = """
        你是一个严谨的量化基本面分析师。请分析研报并输出 JSON:
        {
          "sentiment": float (-1.0 to 1.0),
          "confidence": float (0.0 to 1.0),
          "key_drivers": ["string"],
          "risks": ["string"]
        }
        """
        
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            # Clean possible markdown code blocks
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].strip()
                
            return json.loads(content)
            
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            return {
                "sentiment": 0.0,
                "confidence": 0.0,
                "key_drivers": [],
                "risks": ["Analysis Error"]
            }
            
    # Alias for backward compatibility if needed, but refactoring prefers correct naming
    def analyze_text(self, text: str):
        return self.analyze_report(text)
