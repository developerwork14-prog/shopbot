import os
import re
import sqlite3
import datetime
from html import unescape

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import google.generativeai as genai
from requests.exceptions import JSONDecodeError, RequestException
import requests

load_dotenv()

app = Flask(__name__)
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