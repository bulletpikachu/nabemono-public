from google import genai

from src.config import GOOGLE_API_KEY, GOOGLE_RESPONSE_MODEL

# Center of the UC Irvine campus.
UCI_LATITUDE = 33.6405
UCI_LONGITUDE = -117.8443
UCI_LOCATION_NAME = "UC Irvine"

_client = None


def maps_location(latitude=None, longitude=None):
    if latitude is None and longitude is None:
        return UCI_LATITUDE, UCI_LONGITUDE, UCI_LOCATION_NAME
    if latitude is None or longitude is None:
        raise ValueError("give both latitude and longitude, or neither")
    latitude = float(latitude)
    longitude = float(longitude)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("latitude and longitude are out of range")
    return latitude, longitude, None


def format_maps_response(interaction, *, query, latitude, longitude, location_name):
    texts = []
    sources = []
    seen = set()
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for block in getattr(step, "content", None) or []:
            if getattr(block, "type", None) != "text":
                continue
            text = (getattr(block, "text", None) or "").strip()
            if text:
                texts.append(text)
            for annotation in getattr(block, "annotations", None) or []:
                if getattr(annotation, "type", None) != "place_citation":
                    continue
                source = {
                    "name": getattr(annotation, "name", None),
                    "url": getattr(annotation, "url", None),
                }
                key = (source["name"], source["url"])
                if key in seen:
                    continue
                seen.add(key)
                sources.append(source)

    answer = "\n\n".join(texts).strip()
    if not answer:
        answer = (getattr(interaction, "output_text", None) or "").strip()

    payload = {
        "query": query,
        "location": {
            "latitude": latitude,
            "longitude": longitude,
        },
        "answer": answer,
        "sources": sources,
    }
    if location_name:
        payload["location"]["name"] = location_name
    if sources:
        payload["note"] = (
            "Answer from this result and list each source by name with its Google Maps url."
        )
    elif not answer:
        payload["note"] = "Google Maps returned no answer"
    return payload


def _maps_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GOOGLE_API_KEY)
    return _client


async def search_maps(query, latitude=None, longitude=None, *, client=None, model=None):
    text = (query or "").strip()
    if not text:
        raise ValueError("query must not be empty")
    latitude, longitude, location_name = maps_location(latitude, longitude)
    client = client or _maps_client()
    interaction = await client.aio.interactions.create(
        model=model or GOOGLE_RESPONSE_MODEL,
        input=text,
        tools=[
            {
                "type": "google_maps",
                "latitude": latitude,
                "longitude": longitude,
            }
        ],
    )
    return format_maps_response(
        interaction,
        query=text,
        latitude=latitude,
        longitude=longitude,
        location_name=location_name,
    )
