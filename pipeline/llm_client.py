"""LLM 客户端：兼容国内各大模型 API（OpenAI 兼容格式），供写故事 / 画面策划使用。

支持：deepseek / qwen(阿里百炼) / doubao(火山方舟) / glm(智谱) / kimi(Moonshot) / minimax / ollama
在设置中心或 config.yaml 的 llm 段切换 provider，或直接传 base_url。
"""
from __future__ import annotations

from openai import OpenAI

PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "doubao": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": ""},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3"},
    "minimax": {"base_url": "https://api.minimax.chat/v1", "model": "MiniMax-Text-01"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
}

# 单次 LLM 调用超时（秒）；分镜等长输出给足时间
LLM_TIMEOUT = 180
# 输出上限：分镜 JSON / 长故事需要大 token
LLM_MAX_TOKENS = 8192


def get_client(llm_cfg: dict) -> tuple[OpenAI, str]:
    provider = llm_cfg.get("provider", "deepseek")
    base_url = llm_cfg.get("base_url") or PROVIDERS.get(provider, {}).get("base_url", "")
    model = llm_cfg.get("model") or PROVIDERS.get(provider, {}).get("model", "")
    api_key = llm_cfg.get("api_key") or "ollama" if provider == "ollama" else llm_cfg.get("api_key", "")
    client = OpenAI(base_url=base_url, api_key=api_key or "EMPTY",
                    timeout=float(llm_cfg.get("timeout", LLM_TIMEOUT)),
                    max_retries=int(llm_cfg.get("max_retries", 2)))
    return client, model


def chat_text(llm_cfg: dict, system: str, user: str,
              response_format: dict | None = None) -> str:
    client, model = get_client(llm_cfg)
    provider = llm_cfg.get("provider", "")
    if response_format and response_format.get("type") == "json_object":
        # DashScope requires the lowercase token "json" to appear in messages.
        system = system + "\n请只输出一个合法的 json 对象。"
    temperature = float(llm_cfg.get("temperature", 0.8))
    # Kimi K3 currently only accepts temperature=1.
    if provider == "kimi" and model == "kimi-k3":
        temperature = 1.0
    request = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=int(llm_cfg.get("max_tokens", LLM_MAX_TOKENS)),
    )
    if response_format:
        request["response_format"] = response_format
    resp = client.chat.completions.create(**request)
    return resp.choices[0].message.content or ""
