from unittest.mock import Mock

from intelligence.llm.ollama import OllamaProvider


def test_ollama_provider_allows_slow_cpu_inference_by_default(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {
        "model": "qwen2.5:3b",
        "message": {"content": '{"event_type":"other"}'},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr("intelligence.llm.ollama.requests.post", post)

    provider = OllamaProvider()
    result = provider.generate(system="system", user="user")

    assert result.model == "qwen2.5:3b"
    assert post.call_args.kwargs["timeout"] == 300.0


def test_ollama_provider_respects_explicit_timeout_override():
    provider = OllamaProvider(timeout=12.0)
    assert provider.timeout == 12.0
