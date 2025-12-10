import json
import os
from openai import OpenAI
from config.settings import settings
from loguru import logger

class ResearchAgent:
    def __init__(self, api_key: str = None, base_url: str = "https://api.deepseek.com/v1"):
        self.api_key = api_key or settings.DEEPSEEK_API_KEY
        if not self.api_key:
            logger.warning("DeepSeek API Key not found. LLM features will be disabled or fail.")
        
        self.client = OpenAI(
            api_key=self.api_key or "sk-dummy", # Fallback for testing/mocking
            base_url=base_url
        )

    def analyze_text(self, text: str) -> dict:
        """
        Analyze financial report text using LLM.
        """
        prompt = """
        Role: Financial Analyst. 
        Task: Analyze the provided report text.
        Output JSON keys: 
        - sentiment_score (float, -1.0 to 1.0)
        - key_risks (list of strings)
        - growth_logic (list of strings)
        
        Text to analyze:
        {text}
        
        Return ONLY valid JSON.
        """
        
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat", # Check specific model name for DeepSeek, often just 'deepseek-chat' or 'deepseek-coder'
                messages=[
                    {"role": "system", "content": "You are a helpful financial analyst assistant. Output only JSON."},
                    {"role": "user", "content": prompt.format(text=text)}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            # Clean possible markdown code blocks if model ignores json_object enforcement
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].strip()
                
            return json.loads(content)
            
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            return {
                "sentiment_score": 0.0,
                "key_risks": ["Error processing report"],
                "growth_logic": []
            }
