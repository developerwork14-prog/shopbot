import os
import re
import csv
import sqlite3
import datetime
import functools
from html import unescape
from io import StringIO

from flask import (
    Flask, request, jsonify, render_template_string,
    session, redirect, url_for, make_response, abort
)
from flask_cors import CORS
from dotenv import load_dotenv
import google.generativeai as genai
from requests.exceptions import JSONDecodeError, RequestException
import requests

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))
CORS(app, resources={
    r"/chat": {
        "origins": [
            "https://taffuzo.com",
            "https://www.taffuzo.com",
        ]
    },
    r"/pet-match/*": {
        "origins": [
            "https://taffuzo.com",
            "https://www.taffuzo.com",
        ]
    }
})

WC_URL = "https://taffuzo.com/wp-json/wc/v3/products"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET")
HUMAN_AGENT_WHATSAPP = os.getenv("HUMAN_AGENT_WHATSAPP", "")
HUMAN_AGENT_EMAIL = os.getenv("HUMAN_AGENT_EMAIL", "")

# ── SQLite DB path (persists on Render if you mount a disk, else ephemeral) ──
DB_PATH = os.getenv("DB_PATH", "petmatch.db")


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pet_profiles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_name  TEXT    NOT NULL,
                owner_phone TEXT    NOT NULL,
                pet_name    TEXT    NOT NULL,
                pet_type    TEXT    NOT NULL,   -- 'dog' | 'cat'
                breed       TEXT,
                age_years   REAL,
                gender      TEXT    NOT NULL,   -- 'male' | 'female'
                city        TEXT    NOT NULL,
                bio         TEXT,
                photo_url   TEXT,
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


init_db()


# ─────────────────────────────────────────────────────────────────────────────
# Pet Match endpoints
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ["owner_name", "owner_phone", "pet_name", "pet_type", "gender", "city"]
ALLOWED_PET_TYPES = {"dog", "cat"}
ALLOWED_GENDERS   = {"male", "female"}


@app.route("/pet-match/register", methods=["POST"])
def pet_match_register():
    data = request.get_json(silent=True) or {}

    # Validate required fields
    missing = [f for f in REQUIRED_FIELDS if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    pet_type = str(data["pet_type"]).lower().strip()
    gender   = str(data["gender"]).lower().strip()

    if pet_type not in ALLOWED_PET_TYPES:
        return jsonify({"error": "pet_type must be 'dog' or 'cat'"}), 400
    if gender not in ALLOWED_GENDERS:
        return jsonify({"error": "gender must be 'male' or 'female'"}), 400

    age_years = None
    if data.get("age_years") not in (None, ""):
        try:
            age_years = float(data["age_years"])
        except (ValueError, TypeError):
            return jsonify({"error": "age_years must be a number"}), 400

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pet_profiles
                (owner_name, owner_phone, pet_name, pet_type, breed,
                 age_years, gender, city, bio, photo_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(data["owner_name"]).strip(),
                str(data["owner_phone"]).strip(),
                str(data["pet_name"]).strip(),
                pet_type,
                str(data.get("breed", "")).strip() or None,
                age_years,
                gender,
                str(data["city"]).strip(),
                str(data.get("bio", "")).strip() or None,
                str(data.get("photo_url", "")).strip() or None,
            )
        )
        profile_id = cursor.lastrowid
        conn.commit()

    return jsonify({
        "success": True,
        "id": profile_id,
        "message": (
            f"🐾 {data['pet_name']} is now on Taffuzo Pet Match! "
            "We'll notify you when a match is found."
        )
    }), 201


@app.route("/pet-match/matches/<int:profile_id>", methods=["GET"])
def pet_match_matches(profile_id):
    """
    Returns up to 10 compatible profiles for the given profile_id.
    Compatibility rules:
      - Same pet_type
      - Opposite gender
      - Same city (case-insensitive)
      - Not the same profile
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pet_profiles WHERE id = ?", (profile_id,)
        ).fetchone()

        if not row:
            return jsonify({"error": "Profile not found"}), 404

        opposite_gender = "female" if row["gender"] == "male" else "male"

        matches = conn.execute(
            """
            SELECT id, pet_name, pet_type, breed, age_years, gender, city, bio, photo_url
            FROM   pet_profiles
            WHERE  id       != ?
            AND    pet_type  = ?
            AND    gender    = ?
            AND    LOWER(city) = LOWER(?)
            ORDER  BY created_at DESC
            LIMIT  10
            """,
            (profile_id, row["pet_type"], opposite_gender, row["city"])
        ).fetchall()

    return jsonify({
        "profile_id": profile_id,
        "matches": [dict(m) for m in matches]
    })


@app.route("/pet-match/profiles", methods=["GET"])
def pet_match_profiles():
    """
    List all profiles — optional ?pet_type=dog&city=Bengaluru filters.
    Intentionally does NOT return owner phone — keep that private.
    """
    pet_type = request.args.get("pet_type", "").lower().strip()
    city     = request.args.get("city", "").strip()

    query  = "SELECT id, pet_name, pet_type, breed, age_years, gender, city, bio, photo_url FROM pet_profiles WHERE 1=1"
    params = []

    if pet_type in ALLOWED_PET_TYPES:
        query  += " AND pet_type = ?"
        params.append(pet_type)

    if city:
        query  += " AND LOWER(city) = LOWER(?)"
        params.append(city)

    query += " ORDER BY created_at DESC LIMIT 50"

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    return jsonify({"profiles": [dict(r) for r in rows]})


# ─────────────────────────────────────────────────────────────────────────────
# Everything below is your original ShopBot code — unchanged
# ─────────────────────────────────────────────────────────────────────────────

gemini_api_key = os.getenv("GEMINI_API_KEY")

if gemini_api_key:
    genai.configure(api_key=gemini_api_key)

gemini_model = genai.GenerativeModel(GEMINI_MODEL) if gemini_api_key else None


def human_agent_response():
    contact_lines = []
    if HUMAN_AGENT_WHATSAPP:
        contact_lines.append(f"WhatsApp: {HUMAN_AGENT_WHATSAPP}")
    if HUMAN_AGENT_EMAIL:
        contact_lines.append(f"Email: {HUMAN_AGENT_EMAIL}")
    contact_text = " ".join(contact_lines) if contact_lines else "Our team will help you shortly."
    return (
        "Sure, I can connect you with a human agent. "
        f"{contact_text}"
    )


def clean_text(value):
    text = unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def format_product(product):
    images = product.get("images") or []
    image = images[0].get("src", "") if images else ""
    return {
        "name": product.get("name", ""),
        "price": product.get("price", ""),
        "image": image,
        "url": product.get("permalink", "")
    }


def product_text(product):
    attributes = []
    for attribute in product.get("attributes") or []:
        name = clean_text(attribute.get("name"))
        options = ", ".join(clean_text(option) for option in attribute.get("options") or [])
        if name and options:
            attributes.append(f"{name}: {options}")
    parts = [
        product.get("name", ""),
        product.get("short_description", ""),
        product.get("description", ""),
        " ".join(attributes),
    ]
    return clean_text(" ".join(parts))


def product_source_text(product):
    parts = [
        product.get("short_description", ""),
        product.get("description", ""),
    ]
    return clean_text(" ".join(parts))


def extract_section(text, start_labels, stop_labels):
    lower_text = text.lower()
    starts = [
        lower_text.find(label.lower())
        for label in start_labels
        if lower_text.find(label.lower()) != -1
    ]
    if not starts:
        return ""
    start = min(starts)
    end = len(text)
    for label in stop_labels:
        position = lower_text.find(label.lower(), start + 1)
        if position != -1:
            end = min(end, position)
    return clean_text(text[start:end])


def product_ingredients(product):
    text = product_source_text(product)
    ingredients = extract_section(
        text,
        ["Ingredients:", "Ingredients -", "Ingredient:"],
        ["Made with","Our chef","Supports:","Why our","HealthyPet",
         "Quick changes","Taffuzo","Transitional","Suitable"],
    )
    if not ingredients:
        return ""
    ingredients = re.sub(r"(?i)^ingredients\s*[:\-]\s*", "", ingredients)
    ingredients = re.sub(r"\s*-\s*and\s+that's\s+it!?\s*$", "", ingredients, flags=re.I)
    return clean_text(ingredients)


def fetch_products():
    if not WC_CONSUMER_KEY or not WC_CONSUMER_SECRET:
        raise ValueError("WooCommerce credentials are not configured")
    response = requests.get(
        WC_URL,
        auth=(WC_CONSUMER_KEY, WC_CONSUMER_SECRET),
        headers={"Accept": "application/json"},
        params={"per_page": 50},
        timeout=10
    )
    response.raise_for_status()
    products = response.json()
    if not isinstance(products, list):
        raise ValueError("WooCommerce returned an unexpected response")
    return products


def find_matching_product_records(products, user_message, limit=3):
    words = [
        word
        for word in user_message.lower().replace("-", " ").split()
        if len(word) > 2
    ]
    scored = []
    for product in products:
        name = product.get("name", "").lower()
        description = product_text(product).lower()
        haystack = f"{name} {description}"
        score = sum(1 for word in words if word in haystack)
        if score:
            scored.append((score, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [product for _, product in scored[:limit]]


def find_matching_products(products, user_message):
    return [
        format_product(product)
        for product in find_matching_product_records(products, user_message)
    ]


def is_ingredient_question(user_message):
    message = user_message.lower()
    ingredient_words = [
        "ingredient","ingredients","made of","made from",
        "contains","content","composition","what is used","which are used",
    ]
    return any(word in message for word in ingredient_words)


def is_product_overview_question(user_message):
    message = user_message.lower()
    overview_phrases = [
        "tell me about your products","tell me about you products",
        "what products","your products","you products","product range",
        "what do you sell","what do u sell","show products","show me products",
    ]
    return any(phrase in message for phrase in overview_phrases)


def extract_age_months(user_message):
    message = user_message.lower()
    match = re.search(r"\b(\d{1,2})\s*(month|months|mo)\b", message)
    if match:
        return int(match.group(1))
    year_match = re.search(r"\b(\d{1,2})\s*(year|years|yr|yrs)\b", message)
    if year_match:
        return int(year_match.group(1)) * 12
    return None


def detect_pet_type(user_message):
    message = user_message.lower()
    if "cat" in message or "kitten" in message:
        return "cat"
    if "dog" in message or "puppy" in message:
        return "dog"
    return None


def is_age_question(user_message):
    message = user_message.lower()
    return (
        extract_age_months(user_message) is not None
        or "age" in message
        or "old" in message
        or "puppy" in message
        or "kitten" in message
    )


def format_age(age_months):
    if age_months is None:
        return "this age"
    if age_months % 12 == 0:
        years = age_months // 12
        label = "year" if years == 1 else "years"
        return f"{years} {label}"
    return f"{age_months} months"


def dog_age_answer(age_months):
    if age_months is None:
        return (
            "For dogs, choose food based on age and breed size. Puppies usually need "
            "growth-stage food, adult dogs need adult maintenance food, and senior dogs may "
            "need easier-to-digest food. Switch any new food gradually over 7 days."
        )
    if age_months < 12:
        return (
            f"At {format_age(age_months)}, your dog is usually still in the puppy/growth stage. "
            "Choose puppy or growth-stage dog food, especially for medium and large breeds. "
            "Feed measured portions and change food gradually over 7 days."
        )
    if age_months <= 18:
        return (
            f"At {format_age(age_months)}, your dog is usually ready for adult dog food, especially "
            "if they are a small or medium breed. Large breeds can sometimes stay on "
            "growth-stage food a little longer, so breed size matters. Switch gradually over "
            "7 days and watch for loose stools, vomiting, itching, or low appetite."
        )
    if age_months >= 84:
        return (
            f"At {format_age(age_months)}, your dog is in the senior stage. Choose adult or "
            "senior dog food based on activity level, digestion, dental comfort, and weight. "
            "For joint issues, appetite changes, or illness, check with a vet."
        )
    return (
        f"At {format_age(age_months)}, your dog is an adult. Choose adult dog food based on breed "
        "size, activity level, and any sensitivity. Introduce any new food slowly over 7 days."
    )


def cat_age_answer(age_months):
    if age_months is None:
        return (
            "For cats, choose food based on age first. Kittens need kitten food with higher "
            "protein and calories, adult cats need complete balanced cat food, and senior "
            "cats may need easier-to-digest food. Avoid dog food for cats because cats need "
            "cat-specific nutrients like taurine."
        )
    if age_months < 12:
        return (
            f"At {format_age(age_months)}, your cat is still a kitten. Choose kitten food "
            "with higher protein and calories, feed small measured meals, and introduce new "
            "food slowly over 7 days."
        )
    if age_months >= 84:
        return (
            f"At {format_age(age_months)}, your cat is in the senior stage. Choose complete "
            "cat food that supports digestion and healthy weight, and ask a vet if there are "
            "kidney, dental, appetite, or weight concerns."
        )
    return (
        f"At {format_age(age_months)}, your cat is an adult. Choose complete balanced adult "
        "cat food, introduce new food slowly over 7 days, and avoid dog food because cats "
        "need cat-specific nutrition."
    )


def pet_age_answer(user_message):
    pet_type = detect_pet_type(user_message)
    age_months = extract_age_months(user_message)
    if pet_type == "cat":
        return cat_age_answer(age_months)
    if pet_type == "dog":
        return dog_age_answer(age_months)
    return (
        "Please tell me whether the pet is a dog or cat, plus the age in months or years, "
        "and I can suggest the right feeding stage."
    )


def describe_product_details(product):
    item = format_product(product)
    ingredients = product_ingredients(product)
    details = product_text(product)
    if not ingredients and not details:
        return (
            f"I found {item['name']}, but the ingredient details are not listed in the "
            "product data I can access right now."
        )
    if ingredients:
        ingredient_list = [
            clean_text(ingredient)
            for ingredient in re.split(r",|;", ingredients)
            if clean_text(ingredient)
        ]
        bullets = "\n".join(f"- {ingredient}" for ingredient in ingredient_list[:8])
        return (
            f"{item['name']}\n\n"
            "Ingredients used:\n"
            f"{bullets}\n\n"
            "It is positioned as a natural treat with zero preservatives."
        )
    short_details = " ".join(details.split()[:45])
    return (
        f"{item['name']}\n\n"
        f"Details: {short_details}..."
    )


def product_overview_answer(products):
    product_names = [
        clean_text(product.get("name"))
        for product in products[:6]
        if clean_text(product.get("name"))
    ]
    examples = ""
    if product_names:
        examples = " Some options include " + ", ".join(product_names[:4]) + "."
    return (
        "Taffuzo offers pet food and treats for dogs and cats, including biscuits, "
        "treats, and food options made for everyday feeding and rewards."
        f"{examples} Tell me your pet type, age, and preference, and I can suggest a good option."
    )


def build_catalog_context(products):
    catalog_lines = []
    for product in products[:15]:
        item = format_product(product)
        ingredients = product_ingredients(product)
        details = ingredients or " ".join(product_text(product).split()[:45])
        catalog_lines.append(
            "- "
            f"{item['name']} | Price: INR {item['price'] or 'not listed'} | "
            f"Details: {details or 'not listed'} | URL: {item['url']}"
        )
    return "\n".join(catalog_lines)


def fallback_answer(user_message, products):
    matched_records = find_matching_product_records(products, user_message)
    matched = [format_product(product) for product in matched_records]
    message = user_message.lower()
    if is_product_overview_question(user_message):
        suggestions = [format_product(product) for product in products[:3]]
        return product_overview_answer(products), suggestions
    if matched_records and is_ingredient_question(user_message):
        return describe_product_details(matched_records[0]), matched
    if is_age_question(user_message):
        return pet_age_answer(user_message), matched
    if "cat" in message:
        return cat_age_answer(None), matched
    if "treat" in message or "biscuit" in message:
        return (
            "Treats are best used as a small part of the daily diet, usually under 10% of "
            "daily calories. Look for simple ingredients, avoid too many treats in one day, "
            "and pick the right size for your pet."
        ), matched
    if matched:
        return (
            "Here are a few Taffuzo products that may fit. Tell me your pet type, age, "
            "and preference, and I can narrow it down."
        ), matched
    return (
        "I can help with pet food, treats, and product suggestions. Tell me your pet's age, "
        "breed size, and what you are looking for, and I will suggest a good option."
    ), []


def generate_ai_answer(user_message, products):
    matched_records = find_matching_product_records(products, user_message)
    product_suggestions = [format_product(product) for product in matched_records]

    if matched_records and is_ingredient_question(user_message):
        return describe_product_details(matched_records[0]), product_suggestions, bool(gemini_model)

    if not gemini_model:
        answer, product_suggestions = fallback_answer(user_message, products)
        return answer, product_suggestions, False

    catalog_context = build_catalog_context(products)
    prompt = (
        "You are Taffuzo ShopBot, a friendly AI assistant on Taffuzo.com.\n"
        "Taffuzo sells pet food and treats for dogs and cats. Help customers choose products, "
        "understand feeding, ingredients, treats, shopping, and general pet-food questions.\n"
        "Answer naturally and immediately like a helpful store assistant. Do not behave like "
        "a keyword search engine.\n"
        "If the question is not covered by a fixed rule, still answer using common pet-care "
        "knowledge and the Taffuzo catalog context. If exact Taffuzo information is not in "
        "the catalog, say so briefly and give the best practical guidance.\n"
        "When the customer mentions a dog or cat age, such as 6 months, 13 months, 2 years old, "
        "puppy, or kitten, answer with age-appropriate feeding guidance for that pet type before "
        "suggesting products.\n"
        "When the customer asks about products, what you sell, your catalog, or anything general "
        "about the store, summarize the available products from the catalog clearly and helpfully. "
        "Never say 'I found a few products that may match' - always give a real answer.\n"
        "When the customer asks about a specific product's ingredients, contents, or what it is "
        "made from, answer in 3 to 5 short bullet points. Mention only ingredients and key "
        "benefits. Do not paste the full product description or transition guide.\n"
        "Do not diagnose medical problems. For illness, allergies, pregnancy, poisoning, or serious "
        "symptoms, recommend a veterinarian.\n"
        "Keep answers short, practical, and easy for Indian customers to understand. Prices are "
        "in Indian rupees.\n\n"
        f"Customer question: {user_message}\n\n"
        f"Taffuzo product catalog:\n{catalog_context}"
    )

    response = gemini_model.generate_content(
        prompt,
        generation_config={"max_output_tokens": 350}
    )
    answer = response.text.strip()
    return answer, product_suggestions, True


ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "taffuzo-admin")

# ─────────────────────────────────────────────────────────────────────────────
# Admin helpers
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Taffuzo Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Albert+Sans:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;background:#0d0d0d;display:flex;align-items:center;justify-content:center;font-family:'Albert Sans',sans-serif}
.card{background:#161616;border:1px solid #222;border-radius:18px;padding:44px 40px;width:100%;max-width:380px}
.logo{font-size:22px;font-weight:900;color:#fff;letter-spacing:-0.03em;margin-bottom:4px}
.logo span{color:#ffcc00}
.sub{font-size:12px;color:#555;margin-bottom:32px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase}
label{display:block;font-size:11px;font-weight:700;color:#666;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:6px}
input[type=password]{width:100%;background:#0d0d0d;border:1px solid #2a2a2a;border-radius:10px;padding:12px 14px;color:#fff;font-size:14px;font-family:'Albert Sans',sans-serif;outline:none;transition:border-color .15s}
input[type=password]:focus{border-color:#ffcc00}
.btn{width:100%;margin-top:20px;background:#ffcc00;color:#000;border:none;padding:14px;border-radius:10px;font-size:14px;font-weight:800;font-family:'Albert Sans',sans-serif;cursor:pointer;transition:background .15s}
.btn:hover{background:#e6b800}
.err{background:#1a0000;border:1px solid #3d0000;color:#ff6b6b;border-radius:8px;padding:10px 14px;font-size:12px;margin-top:14px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Taffuzo<span>.</span></div>
  <div class="sub">Admin Panel</div>
  <form method="POST">
    <label>Password</label>
    <input type="password" name="password" autofocus placeholder="Enter admin password">
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <button class="btn" type="submit">Sign In →</button>
  </form>
</div>
</body>
</html>"""


ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pet Match Admin · Taffuzo</title>
<link href="https://fonts.googleapis.com/css2?family=Albert+Sans:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d0d0d;--surface:#161616;--border:#222;--border2:#2a2a2a;
  --text:#f0f0f0;--muted:#666;--accent:#ffcc00;--accent-dim:#3d3000;
  --danger:#ff4444;--danger-dim:#2a0a0a;
  --dog:#4d9fff;--cat:#c084fc;
  --male:#60d9fa;--female:#f9a8d4;
}
body{min-height:100vh;background:var(--bg);font-family:'Albert Sans',sans-serif;color:var(--text)}

/* ── Top bar ── */
.topbar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:0 28px;height:58px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;
}
.topbar-logo{font-size:18px;font-weight:900;color:#fff;letter-spacing:-0.03em}
.topbar-logo span{color:var(--accent)}
.topbar-right{display:flex;align-items:center;gap:12px}
.badge-live{
  background:var(--accent-dim);color:var(--accent);border:1px solid #6b5000;
  padding:4px 10px;border-radius:999px;font-size:11px;font-weight:700;
  letter-spacing:0.05em;
}
.topbar-logout{
  background:none;border:1px solid var(--border2);color:var(--muted);
  padding:6px 14px;border-radius:8px;cursor:pointer;font-size:12px;
  font-family:'Albert Sans',sans-serif;font-weight:600;transition:all .15s;
}
.topbar-logout:hover{border-color:var(--danger);color:var(--danger)}

/* ── Layout ── */
.container{max-width:1200px;margin:0 auto;padding:28px}

/* ── Stat cards ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
.stat-label{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:8px}
.stat-value{font-size:28px;font-weight:900;color:var(--text);line-height:1}
.stat-value.accent{color:var(--accent)}
.stat-value.dog{color:var(--dog)}
.stat-value.cat{color:var(--cat)}

/* ── Toolbar ── */
.toolbar{
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;margin-bottom:16px;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
}
.filter-select,.search-input{
  background:var(--bg);border:1px solid var(--border2);border-radius:8px;
  padding:8px 12px;color:var(--text);font-size:12px;font-family:'Albert Sans',sans-serif;
  outline:none;transition:border-color .15s;
}
.filter-select:focus,.search-input:focus{border-color:var(--accent)}
.filter-select option{background:#1a1a1a}
.search-input{flex:1;min-width:160px}
.search-input::placeholder{color:var(--muted)}
.toolbar-gap{flex:1}
.csv-btn{
  background:var(--accent);color:#000;border:none;padding:8px 18px;border-radius:8px;
  cursor:pointer;font-size:12px;font-weight:800;font-family:'Albert Sans',sans-serif;
  transition:background .15s;white-space:nowrap;text-decoration:none;display:inline-flex;align-items:center;gap:6px;
}
.csv-btn:hover{background:#e6b800}

/* ── Table ── */
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13px}
thead{background:#111}
th{
  padding:12px 14px;text-align:left;font-size:10px;font-weight:700;
  color:var(--muted);letter-spacing:0.07em;text-transform:uppercase;border-bottom:1px solid var(--border);
}
td{padding:12px 14px;border-bottom:1px solid var(--border2);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,0.02)}

.pill{
  display:inline-flex;align-items:center;gap:5px;padding:3px 9px;
  border-radius:999px;font-size:11px;font-weight:700;
}
.pill-dog{background:rgba(77,159,255,0.12);color:var(--dog)}
.pill-cat{background:rgba(192,132,252,0.12);color:var(--cat)}
.pill-male{background:rgba(96,217,250,0.1);color:var(--male)}
.pill-female{background:rgba(249,168,212,0.1);color:var(--female)}

.owner-name{font-weight:700;color:var(--text)}
.owner-phone{font-size:11px;color:var(--muted);margin-top:2px}
.pet-name{font-weight:800;color:var(--text)}
.pet-breed{font-size:11px;color:var(--muted);margin-top:1px}
.city-text{color:var(--text);font-weight:600}
.date-text{font-size:11px;color:var(--muted)}
.bio-text{font-size:11px;color:var(--muted);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.del-btn{
  background:none;border:1px solid var(--border2);color:var(--muted);
  width:28px;height:28px;border-radius:7px;cursor:pointer;font-size:14px;
  display:flex;align-items:center;justify-content:center;transition:all .15s;
}
.del-btn:hover{background:var(--danger-dim);border-color:var(--danger);color:var(--danger)}

.empty-row td{text-align:center;padding:48px;color:var(--muted);font-size:13px}
.empty-icon{font-size:32px;display:block;margin-bottom:10px}

/* count */
.result-count{font-size:11px;color:var(--muted);margin-bottom:10px;font-weight:600}
.result-count strong{color:var(--text)}

/* ── Confirm modal ── */
.modal-bg{
  position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;
  display:none;align-items:center;justify-content:center;
}
.modal-bg.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px;width:340px;max-width:90vw}
.modal h3{font-size:16px;font-weight:800;margin-bottom:8px}
.modal p{font-size:13px;color:var(--muted);line-height:1.5;margin-bottom:20px}
.modal-actions{display:flex;gap:10px}
.modal-cancel{flex:1;background:none;border:1px solid var(--border2);color:var(--muted);padding:10px;border-radius:8px;cursor:pointer;font-family:'Albert Sans',sans-serif;font-weight:700;font-size:13px;transition:all .15s}
.modal-cancel:hover{border-color:var(--text);color:var(--text)}
.modal-confirm{flex:1;background:var(--danger);color:#fff;border:none;padding:10px;border-radius:8px;cursor:pointer;font-family:'Albert Sans',sans-serif;font-weight:800;font-size:13px;transition:background .15s}
.modal-confirm:hover{background:#cc2222}

@media(max-width:900px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .container{padding:16px}
  table{font-size:12px}
  td,th{padding:10px}
}
@media(max-width:600px){
  .stats{grid-template-columns:1fr 1fr}
  .hide-mobile{display:none}
}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-logo">Taffuzo<span>.</span> <span style="font-size:13px;color:#555;font-weight:600">Pet Match Admin</span></div>
  <div class="topbar-right">
    <div class="badge-live">● LIVE</div>
    <form method="POST" action="/admin/logout" style="margin:0">
      <button class="topbar-logout" type="submit">Sign out</button>
    </form>
  </div>
</div>

<div class="container">

  <!-- Stats -->
  <div class="stats">
    <div class="stat">
      <div class="stat-label">Total Registrations</div>
      <div class="stat-value accent">{{ total }}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Dogs</div>
      <div class="stat-value dog">{{ dogs }}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Cats</div>
      <div class="stat-value cat">{{ cats }}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Cities</div>
      <div class="stat-value">{{ cities }}</div>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <input class="search-input" type="text" id="search" placeholder="Search by name, owner, city…" oninput="filterTable()">
    <select class="filter-select" id="filter-type" onchange="filterTable()">
      <option value="">All pets</option>
      <option value="dog">🐶 Dogs</option>
      <option value="cat">🐱 Cats</option>
    </select>
    <select class="filter-select" id="filter-gender" onchange="filterTable()">
      <option value="">All genders</option>
      <option value="male">Male</option>
      <option value="female">Female</option>
    </select>
    <select class="filter-select" id="filter-city" onchange="filterTable()">
      <option value="">All cities</option>
      {% for city in city_list %}
      <option value="{{ city|lower }}">{{ city }}</option>
      {% endfor %}
    </select>
    <div class="toolbar-gap"></div>
    <a class="csv-btn" href="/admin/export-csv">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Export CSV
    </a>
  </div>

  <div class="result-count" id="result-count"></div>

  <!-- Table -->
  <div class="table-wrap">
    <table id="profiles-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Owner</th>
          <th>Pet</th>
          <th>Type</th>
          <th>Gender</th>
          <th class="hide-mobile">Age</th>
          <th>City</th>
          <th class="hide-mobile">Bio</th>
          <th class="hide-mobile">Registered</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="table-body">
        {% if profiles %}
          {% for p in profiles %}
          <tr
            data-name="{{ p.pet_name|lower }}"
            data-owner="{{ p.owner_name|lower }}"
            data-city="{{ p.city|lower }}"
            data-type="{{ p.pet_type }}"
            data-gender="{{ p.gender }}"
          >
            <td style="color:var(--muted);font-size:11px">{{ p.id }}</td>
            <td>
              <div class="owner-name">{{ p.owner_name }}</div>
              <div class="owner-phone">{{ p.owner_phone }}</div>
            </td>
            <td>
              <div class="pet-name">{{ p.pet_name }}</div>
              {% if p.breed %}<div class="pet-breed">{{ p.breed }}</div>{% endif %}
            </td>
            <td>
              <span class="pill pill-{{ p.pet_type }}">
                {% if p.pet_type == 'dog' %}🐶{% else %}🐱{% endif %}
                {{ p.pet_type|capitalize }}
              </span>
            </td>
            <td>
              <span class="pill pill-{{ p.gender }}">{{ p.gender|capitalize }}</span>
            </td>
            <td class="hide-mobile">{{ p.age_years ~ ' yrs' if p.age_years else '—' }}</td>
            <td><span class="city-text">{{ p.city }}</span></td>
            <td class="hide-mobile"><div class="bio-text" title="{{ p.bio or '' }}">{{ p.bio or '—' }}</div></td>
            <td class="hide-mobile"><div class="date-text">{{ p.created_at[:10] if p.created_at else '—' }}</div></td>
            <td>
              <button class="del-btn" onclick="confirmDelete({{ p.id }}, '{{ p.pet_name }}')" title="Delete">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
              </button>
            </td>
          </tr>
          {% endfor %}
        {% else %}
          <tr class="empty-row"><td colspan="10"><span class="empty-icon">🐾</span>No registrations yet.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
</div>

<!-- Delete confirm modal -->
<div class="modal-bg" id="del-modal">
  <div class="modal">
    <h3>Delete profile?</h3>
    <p id="del-modal-msg">This will permanently remove the pet from Pet Match.</p>
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm" id="del-confirm-btn">Delete</button>
    </div>
  </div>
</div>

<script>
let deleteId = null;

function confirmDelete(id, name) {
    deleteId = id;
    document.getElementById("del-modal-msg").textContent =
        `This will permanently remove "${name}" from Pet Match. This cannot be undone.`;
    document.getElementById("del-modal").classList.add("open");
}
function closeModal() {
    document.getElementById("del-modal").classList.remove("open");
    deleteId = null;
}
document.getElementById("del-confirm-btn").onclick = async function() {
    if (!deleteId) return;
    this.textContent = "Deleting…";
    this.disabled = true;
    try {
        const resp = await fetch(`/admin/delete/${deleteId}`, { method: "POST" });
        if (resp.ok) {
            const row = document.querySelector(`tr[data-name]`);
            // Remove row with matching id
            const allRows = document.querySelectorAll("#table-body tr");
            allRows.forEach(r => {
                const delBtn = r.querySelector(".del-btn");
                if (delBtn && delBtn.getAttribute("onclick").includes(`(${deleteId},`)) {
                    r.remove();
                }
            });
            closeModal();
            updateCount();
        } else {
            alert("Delete failed. Please try again.");
        }
    } catch(e) {
        alert("Error: " + e.message);
    }
    this.textContent = "Delete";
    this.disabled = false;
};

function filterTable() {
    const search = document.getElementById("search").value.toLowerCase();
    const type   = document.getElementById("filter-type").value;
    const gender = document.getElementById("filter-gender").value;
    const city   = document.getElementById("filter-city").value;
    let visible  = 0;

    document.querySelectorAll("#table-body tr[data-name]").forEach(row => {
        const matchSearch = !search ||
            row.dataset.name.includes(search) ||
            row.dataset.owner.includes(search) ||
            row.dataset.city.includes(search);
        const matchType   = !type   || row.dataset.type   === type;
        const matchGender = !gender || row.dataset.gender === gender;
        const matchCity   = !city   || row.dataset.city   === city;

        const show = matchSearch && matchType && matchGender && matchCity;
        row.style.display = show ? "" : "none";
        if (show) visible++;
    });
    updateCount(visible);
}

function updateCount(n) {
    const allRows = document.querySelectorAll("#table-body tr[data-name]").length;
    const count   = (n === undefined) ? allRows : n;
    document.getElementById("result-count").innerHTML =
        `Showing <strong>${count}</strong> registration${count !== 1 ? "s" : ""}`;
}

document.getElementById("del-modal").addEventListener("click", function(e) {
    if (e.target === this) closeModal();
});

updateCount();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = False
            return redirect(url_for("admin_dashboard"))
        error = "Incorrect password. Please try again."

    return render_template_string(ADMIN_LOGIN_HTML, error=error)


@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    with get_db() as conn:
        profiles = [dict(r) for r in conn.execute(
            "SELECT * FROM pet_profiles ORDER BY created_at DESC"
        ).fetchall()]

        total  = len(profiles)
        dogs   = sum(1 for p in profiles if p["pet_type"] == "dog")
        cats   = sum(1 for p in profiles if p["pet_type"] == "cat")
        cities = len({p["city"].strip().lower() for p in profiles if p.get("city")})
        city_list = sorted({p["city"].strip() for p in profiles if p.get("city")},
                           key=lambda c: c.lower())

    return render_template_string(
        ADMIN_DASHBOARD_HTML,
        profiles=profiles,
        total=total, dogs=dogs, cats=cats, cities=cities,
        city_list=city_list,
    )


@app.route("/admin/delete/<int:profile_id>", methods=["POST"])
@login_required
def admin_delete(profile_id):
    with get_db() as conn:
        conn.execute("DELETE FROM pet_profiles WHERE id = ?", (profile_id,))
        conn.commit()
    return jsonify({"success": True})


@app.route("/admin/export-csv")
@login_required
def admin_export_csv():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, owner_name, owner_phone, pet_name, pet_type, "
            "breed, age_years, gender, city, bio, photo_url, created_at "
            "FROM pet_profiles ORDER BY created_at DESC"
        ).fetchall()

    si = StringIO()
    writer = csv.writer(si)
    writer.writerow([
        "ID", "Owner Name", "Owner Phone", "Pet Name", "Pet Type",
        "Breed", "Age (years)", "Gender", "City", "Bio", "Photo URL", "Registered At"
    ])
    for row in rows:
        writer.writerow(list(row))

    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = (
        f"attachment; filename=petmatch_export_{datetime.date.today()}.csv"
    )
    output.headers["Content-type"] = "text/csv"
    return output


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Send JSON like {'message': 'shirt'}"}), 400

    if user_message.lower() in {
        "request_human_agent","human agent","agent",
        "talk to human","talk to agent",
    }:
        return jsonify({
            "answer": human_agent_response(),
            "products": [],
            "ai_enabled": bool(gemini_model),
            "action": "human_agent"
        })

    try:
        products = fetch_products()
    except JSONDecodeError:
        return jsonify({"error": "WooCommerce did not return JSON"}), 502
    except RequestException as error:
        return jsonify({"error": "WooCommerce request failed", "details": str(error)}), 502
    except ValueError as error:
        return jsonify({"error": str(error)}), 502

    try:
        answer, suggested_products, ai_enabled = generate_ai_answer(user_message, products)
    except Exception as error:
        answer, suggested_products = fallback_answer(user_message, products)
        print(f"AI answer is temporarily unavailable: {error}")
        ai_enabled = False

    return jsonify({
        "answer": answer,
        "products": suggested_products,
        "ai_enabled": ai_enabled
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "ai_enabled": bool(gemini_model),
        "provider": "gemini",
        "model": GEMINI_MODEL
    })


@app.route("/")
def home():
    return """
    <h1>Shopbot is running</h1>
    <p>Send a POST request to <code>/chat</code> with JSON:</p>
    <pre>{"message": "shirt"}</pre>
    <p>Pet Match: POST /pet-match/register | GET /pet-match/matches/&lt;id&gt; | GET /pet-match/profiles</p>
    """


if __name__ == "__main__":
    app.run(debug=True)