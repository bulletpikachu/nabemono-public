import json

from src.config import (
    GOOGLE_API_KEY,
    GOOGLE_RESPONSE_MODEL,
    OPENROUTER_API_KEY,
    RESPONSE_MODEL,
    RESPONSE_PROVIDER,
)


def ping_openrouter():
    import requests

    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": RESPONSE_MODEL,
            "messages": [
                {"role": "user", "content": "Hello"},
            ],
        },
    )
    response.raise_for_status()
    return response


def ping_google():
    from google import genai

    client = genai.Client(api_key=GOOGLE_API_KEY)
    return client.models.generate_content(
        model=GOOGLE_RESPONSE_MODEL,
        contents="Hello",
    )


def ping():
    print(f"[PROVIDER]: {RESPONSE_PROVIDER}")
    if RESPONSE_PROVIDER == "openrouter":
        response = ping_openrouter()
        print(response.status_code)
        print(json.dumps(dict(response.headers), indent=2))
        print(json.dumps(response.json(), indent=2))
        return

    response = ping_google()
    print(response.text)
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        print(usage.model_dump() if hasattr(usage, "model_dump") else usage)


if __name__ == "__main__":
    try:
        ping()
    except Exception as error:
        response = getattr(error, "response", None)
        if response is not None and getattr(response, "status_code", None) == 429:
            print(response.status_code)
            print(json.dumps(dict(response.headers), indent=2))
            try:
                print(json.dumps(response.json(), indent=2))
            except ValueError:
                print(response.text)
        else:
            raise
