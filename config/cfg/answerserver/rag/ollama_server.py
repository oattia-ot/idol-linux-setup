import os
from typing import Tuple
import requests
from transformers import AutoTokenizer

# Use /api/chat — correct for messages[] format
LLM_ENDPOINT = os.getenv('IDOL_LLM_ENDPOINT') or 'http://ollama:11434/api/chat'
LLM_MODEL    = os.getenv('IDOL_LLM_MODEL')    or 'gemma-4-e4b:latest'


def generate(prompt: str) -> str:
    if 'Question:' in prompt:
        context, question = prompt.split('Question:', 1)
    else:
        context  = ''
        question = prompt

    chat_history = question.split('+')
    messages = [{"role": "system", "content": context.strip()}]

    n = 0
    for chat in chat_history:
        n += 1
        role = "user" if n % 2 == 1 else "assistant"
        if chat.strip():
            messages.append({"role": role, "content": chat.strip()})

    if not any(m["role"] == "user" for m in messages):
        messages.append({"role": "user", "content": prompt.strip()})

    headers = {'Content-Type': 'application/json'}
    # OLLAMA_CONFIG_BEGIN
    data = {
        "model":   LLM_MODEL,
        "messages": messages,
        "stream":  False,
        "options": {
            "temperature": 0,
            "num_predict": 512,
            "num_ctx":     4096,
        }
    }
    # OLLAMA_CONFIG_END
    
    total_chars = sum(len(m['content']) for m in messages)
    print(f"[DEBUG] Endpoint={LLM_ENDPOINT}, model={LLM_MODEL}, prompt_chars={total_chars}")
    print(f"[DEBUG] Messages: {messages}")

    response = requests.post(LLM_ENDPOINT, headers=headers, json=data, timeout=300)
    response.raise_for_status()

    response_json = response.json()
    print(f"[DEBUG] Response: {response_json}")

    if 'message' not in response_json:
        raise RuntimeError(f"Unable to find 'message' in response:\n{response.text}")

    if 'content' not in (message := response_json['message']):
        raise RuntimeError(f"'content' not found in 'message':\n{response.text}")

    content = message['content'].strip()

    if not content:
        raise RuntimeError(
            f"Model returned empty content. done_reason={response_json.get('done_reason')}. "
            f"Messages sent: {messages}"
        )

    return content


def get_token_count(text: str, token_limit: int) -> Tuple[str, int]:
    tokenizer_cache_dir = os.path.join(os.path.dirname(__file__), "tokenizer_cache")
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_cache_dir)

    chat_completion_tokenized = tokenizer.encode(f'[INST] {text} [/INST]', add_special_tokens=True)
    original_token_count = len(chat_completion_tokenized)

    truncated_text = text
    if original_token_count > token_limit:
        tokenized_text_no_specials = tokenizer.encode(text, add_special_tokens=False)
        special_token_count = original_token_count - len(tokenized_text_no_specials)
        truncated_text_token_limit = max(token_limit - special_token_count, 1)
        truncated_text_tokenized   = tokenized_text_no_specials[:truncated_text_token_limit]
        truncated_text             = tokenizer.decode(truncated_text_tokenized, clean_up_tokenization_spaces=True)

    return truncated_text, original_token_count
