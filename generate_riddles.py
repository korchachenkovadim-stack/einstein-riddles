"""Генератор загадок для Everyone Like Einstein.

Запускається на твоєму комп'ютері або за розкладом (cron, GitHub Actions), не в грі.
Додає нові загадки в riddles.json (старі зберігає) для всіх мов. Готовий файл виклади
за https-адресою, яку гра читає з EINSTEIN_RIDDLES_URL.

    pip install google-genai
    GEMINI_API_KEY=...  python generate_riddles.py [riddles.json] [скільки_на_мову]
"""
import json
import os
import random
import re
import sys
import time
import unicodedata

from google import genai

LANGS = ["UA", "EN", "DE", "ES"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
OUT = sys.argv[1] if len(sys.argv) > 1 else "riddles.json"
PER_LANG = int(sys.argv[2]) if len(sys.argv) > 2 else 20
BATCH = int(os.environ.get("GEMINI_BATCH", "40"))  # загадок за один запит: безкоштовний тариф дає лише ~20 запитів на добу
MAX_TOTAL = 30000
STALE_LIMIT = 10  # стільки запитів поспіль без жодного нового слова, і мова вважається вичерпаною
TOPICS = [
    "general science",
    "physics",
    "history (events, eras, famous people, inventions)",
    "space and astronomy",
    "chemistry",
    "mathematics (numbers, shapes, simple concepts)",
    "animals",
    "plants",
    "riddles for young children (very simple, friendly, everyday things a child knows)",
    "logic riddles (wordplay and lateral thinking, still with a one-word answer)",
]


def norm(word):
    s = unicodedata.normalize("NFKD", str(word).upper())
    return "".join(ch for ch in s if ch.isalnum())


class OutOfQuota(Exception):
    pass


_models = [MODEL]
_mi = 0
_discovered = False


def discover_models(client):
    """Інші flash/lite моделі цього ключа: ліміт на добу рахується окремо для кожної моделі."""
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").removeprefix("models/")
            if "generateContent" not in (getattr(m, "supported_actions", None) or []):
                continue
            if "gemini" not in name or not ("flash" in name or "lite" in name):
                continue
            if any(bad in name for bad in ("image", "tts", "live", "audio", "embedding", "robotics")):
                continue
            mt = re.search(r"gemini-(\d+(?:\.\d+)*)", name)
            ver = tuple(int(x) for x in mt.group(1).split(".")) if mt else (0,)
            found.append((ver, name))
    except Exception as e:
        print(f"  Не вдалося отримати список моделей: {e}", file=sys.stderr)
    found.sort(reverse=True)
    return [n for _, n in found][:4]


def switch_model(client, why):
    global _mi, _discovered
    if not _discovered:
        _discovered = True
        for n in discover_models(client):
            if n not in _models:
                _models.append(n)
    if _mi + 1 < len(_models):
        _mi += 1
        print(f"  Модель {_models[_mi - 1]}: {why}. Пробую {_models[_mi]}", file=sys.stderr)
        return True
    return False


def ask(client, lang, count, topic):
    prompt = (
        f"Generate exactly {count} unique riddles in language '{lang}' "
        f"on the topic: {topic}. Avoid the most obvious words. "
        "Each riddle needs a single-word answer of 3 to 10 letters (no spaces, no hyphens) and a short clue "
        "starting with a category in square brackets. Return ONLY a JSON list, no markdown, like: "
        '[{"word": "ATOM", "riddle": "[Physics] Smallest particle of an element."}]'
    )
    attempt = 0
    while attempt < 6:
        try:
            text = (client.models.generate_content(model=_models[_mi], contents=prompt).text or "").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text).strip()
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception as e:  # 503, квота (429), невалідний JSON
            msg = str(e)
            err = msg.upper()
            print(f"  {lang}: {_models[_mi]}: {msg[:160]}", file=sys.stderr)
            if "404" in err or "NOT_FOUND" in err:
                if switch_model(client, "недоступна"):
                    continue
                sys.exit("Жодна модель не доступна. Задай назву вручну: $env:GEMINI_MODEL=\"назва\"")
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                if "PERDAY" in err or "PER DAY" in err:  # добовий ліміт: чекати хвилини немає сенсу
                    if switch_model(client, "добовий ліміт вичерпано"):
                        continue
                    m = re.search(r"retry in ([0-9hms.]+)", msg)
                    raise OutOfQuota(m.group(1) if m else "")
                wait = 30  # ліміт на хвилину
            elif "503" in err or "UNAVAILABLE" in err or "OVERLOADED" in err or "HIGH DEMAND" in err:
                wait = 10  # модель перевантажена: це минає
            else:
                wait = 2
            attempt += 1
            time.sleep(wait * attempt + random.random())
    return None  # усі спроби невдалі (на відміну від [] коли модель відповіла, але нового немає)


def acceptable(word, riddle):
    """Фільтр для повністю автоматичного режиму: людина ці загадки не читає."""
    w, r = word.strip(), riddle.strip()
    key = norm(w)
    if not w.isalpha() or not 3 <= len(key) <= 10:       # одне слово з літер, без цифр і символів
        return False
    if not (r.startswith("[") and "]" in r[:40]) or len(r) > 300 or len(r) < 12:  # категорія в дужках
        return False
    if key in norm(r.split("]", 1)[1]):                  # підказка не має містити саму відповідь
        return False
    return True


def save(data):
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data[-MAX_TOTAL:], f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)


def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Задай змінну середовища GEMINI_API_KEY")
    try:
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    have = {(str(d.get("lang", "UA")).upper(), norm(d.get("word", ""))) for d in data if isinstance(d, dict)}
    client = genai.Client(api_key=key)
    added = 0
    for lang in LANGS:
        got, stale, n, fails = 0, 0, 0, 0
        while got < PER_LANG and stale < STALE_LIMIT:
            topic = TOPICS[(n + LANGS.index(lang) * 7) % len(TOPICS)]
            n += 1
            before = got
            try:
                items = ask(client, lang, min(BATCH, PER_LANG - got), topic)
            except OutOfQuota as q:
                save(data)
                sys.exit(f"Добовий ліміт безкоштовного тарифу вичерпано на всіх моделях. Збережено {len(data)} записів. "
                         f"Запусти скрипт знову приблизно через {str(q).rstrip('.') or 'кілька годин'}, він продовжить.")
            if items is None:  # збій API не рахуємо як «слова закінчились»
                fails += 1
                if fails >= 4:
                    save(data)
                    sys.exit(f"API не відповідає. Збережено {len(data)} записів. Запусти скрипт пізніше, він продовжить.")
                continue
            fails = 0
            for x in items:
                if not (isinstance(x, dict) and isinstance(x.get("word"), str) and isinstance(x.get("riddle"), str)):
                    continue
                k = (lang, norm(x["word"]))
                if k in have or not acceptable(x["word"], x["riddle"]):
                    continue
                have.add(k)
                data.append({"word": x["word"].strip().upper(), "riddle": x["riddle"].strip(), "lang": lang})
                got += 1
                added += 1
            stale = 0 if got > before else stale + 1
            if n % 5 == 0:  # проміжне збереження: збій чи ліміт API не з'їдять зроблене
                save(data)
                print(f"  {lang}: {got}/{PER_LANG}", flush=True)
        print(f"{lang}: +{got}" + (" (нових слів майже не лишилось)" if stale >= STALE_LIMIT else ""))
        save(data)
    print(f"Готово: +{added}, усього {len(data)} у {OUT}")


if __name__ == "__main__":
    main()
