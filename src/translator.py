import os
import requests

DEFAULT_ENDPOINTS=[
    "https://translate.cutie.dating/translate",
    "https://translate.fedilab.app/translate"
]

class TranslationError(Exception):
    pass

def endpoints():
    raw=os.getenv("TRANSLATE_ENDPOINTS","").strip()
    if raw:
        return [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]
    return DEFAULT_ENDPOINTS

def translate_to_english(text):
    text=(text or "").strip()
    if not text:
        raise TranslationError("empty text")

    timeout=float(os.getenv("TRANSLATE_TIMEOUT","12"))
    last=None

    for endpoint in endpoints():
        try:
            r=requests.post(
                endpoint,
                json={
                    "q":text[:5000],
                    "source":"auto",
                    "target":"en",
                    "format":"text"
                },
                headers={"Content-Type":"application/json"},
                timeout=timeout
            )
            if r.status_code>=300:
                last=f"{endpoint}: HTTP {r.status_code}"
                continue
            data=r.json()
            translated=(data.get("translatedText") or "").strip()
            detected=(data.get("detectedLanguage") or {})
            if translated:
                return {
                    "text":translated,
                    "detected_language":detected.get("language"),
                    "confidence":detected.get("confidence"),
                    "endpoint":endpoint
                }
            last=f"{endpoint}: no translatedText"
        except Exception as exc:
            last=f"{endpoint}: {exc}"

    raise TranslationError(last or "no translation endpoint available")
