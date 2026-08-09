import re

COMMON_ENGLISH={
"the","and","of","to","in","for","on","with","from","after","has","have",
"was","were","is","are","will","new","news","government","official","report",
"said","says","according","world","market","company","today","people"
}

def english_score(text):
    words=re.findall(r"[A-Za-z']+",text or "")
    if not words:return 0
    hits=sum(1 for w in words if w.lower() in COMMON_ENGLISH)
    ascii_ratio=sum(1 for c in text if ord(c)<128)/max(1,len(text))
    return (hits/max(1,min(len(words),30)))*.75+ascii_ratio*.25

def is_english(text):
    return english_score(text)>=.18

def check_item(item):
    combined=(item.get("title","")+" "+item.get("summary","")).strip()
    return "ENGLISH" if is_english(combined) else "TRANSLATION_REQUIRED"
