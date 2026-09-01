from unittest.mock import MagicMock, patch

from src.llm_agent.analyzer import ResearchAgent


def test_analyze_text_mock():
    # Mock OpenAI client（注意 patch 路径必须是被测模块自身的命名空间）
    with patch("src.llm_agent.analyzer.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = """
        {
            "sentiment": 0.8,
            "confidence": 0.9,
            "key_drivers": ["Market expansion"],
            "risks": ["Policy uncertainty"]
        }
        """
        mock_client.chat.completions.create.return_value = mock_completion

        agent = ResearchAgent(api_key="test_key")
        result = agent.analyze_text("Some report content")

        assert result["sentiment"] == 0.8
        assert result["confidence"] == 0.9
        assert "Policy uncertainty" in result["risks"]
