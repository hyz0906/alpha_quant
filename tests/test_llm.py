import pytest
from unittest.mock import MagicMock, patch
from src.analysis.llm_agent import ResearchAgent

def test_analyze_text_mock():
    # Mock OpenAI client
    with patch('src.analysis.llm_agent.OpenAI') as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        
        # Mock response
        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = """
        {
            "sentiment_score": 0.8,
            "key_risks": ["Policy uncertainty"],
            "growth_logic": ["Market expansion"]
        }
        """
        mock_client.chat.completions.create.return_value = mock_completion
        
        agent = ResearchAgent(api_key="test_key")
        result = agent.analyze_text("Some report content")
        
        assert result['sentiment_score'] == 0.8
        assert "Policy uncertainty" in result['key_risks']
