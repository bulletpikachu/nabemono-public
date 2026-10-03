import requests
from src.prompts import load_affection_classifier
from src.config import AFFECTION_MODEL, GROQ_API_KEY
from groq import Groq

groq_client = Groq(api_key=GROQ_API_KEY)

async def affection_classifier(message):
    prompt = load_affection_classifier().replace("{message}", message.content)
    response = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": prompt},
        ],
        model=AFFECTION_MODEL,
        temperature=0.2,
        max_completion_tokens=1024,
    )

    affection = response.choices[0].message.content.strip()
    print(f"[AFFECTION]: {affection}")
    return affection