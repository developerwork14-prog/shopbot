import os
import re
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
    }
})

WC_URL = "https://taffuzo.com/wp-json/wc/v3/products"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET")
HUMAN_AGENT_WHATSAPP = os.getenv("HUMAN_AGENT_WHATSAPP", "")
HUMAN_AGENT_EMAIL = os.getenv("HUMAN_AGENT_EMAIL", "")

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

    if contact_lines:
        contact_text = " ".join(contact_lines)
    else:
        contact_text = "Our team will help you shortly."

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
        "ingredient",
        "ingredients",
        "made of",
        "made from",
        "contains",
        "content",
        "composition",
        "what is used",
        "which are used",
    ]

    return any(word in message for word in ingredient_words)


def is_product_overview_question(user_message):
    message = user_message.lower()
    overview_phrases = [
        "tell me about your products",
        "tell me about you products",
        "what products",
        "your products",
        "you products",
        "product range",
        "what do you sell",
        "what do u sell",
        "show products",
        "show me products",
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
    details = product_text(product)
    item = format_product(product)

    if not details:
        return (
            f"I found {item['name']}, but the ingredient details are not listed in the "
            "product data I can access right now."
        )

    return f"For {item['name']}: {details}"


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
        details = product_text(product)
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


def generate_ai_answer_legacy(user_message, products):
    if not gemini_model:
        answer, product_suggestions = fallback_answer(user_message, products)
        return answer, product_suggestions, False

    product_suggestions = find_matching_products(products, user_message)
    catalog_context = build_catalog_context(products)
    prompt = (
    "You are Taffuzo ShopBot, a friendly AI assistant on Taffuzo.com.\n"
    "Answer customer questions naturally and immediately, like a helpful store assistant.\n"
    "When the customer mentions a dog or cat age, such as 6 months, 13 months, "
    "2 years old, puppy, or kitten, answer with age-appropriate feeding guidance "
    "for that pet type before suggesting products.\n"
    "When the customer asks about products, what you sell, your catalog, or anything general "
    "about the store, summarize the available products from the catalog clearly and helpfully. "
    "Never say 'I found a few products that may match' — always give a real answer.\n"
        "When the customer asks about a specific product's ingredients, contents, or what it "
        "is made from, answer with the ingredient/details information from the product catalog. "
        "Do not only say that products match the question.\n"
        "When the customer asks broadly about Taffuzo products or what Taffuzo sells, summarize "
        "the main product range and mention a few relevant examples from the catalog.\n"
        "If the question is about pets, dog food, cat food, treats, feeding, ingredients, "
    "product choice, orders, or shopping, give a direct useful answer.\n"
    "Use the Taffuzo product catalog when it helps, but do not say you can only search products.\n"
    "If the question is general and not about Taffuzo, still answer briefly and politely, "
    "then connect back to pets or shopping if useful.\n"
    "Do not diagnose medical problems. For illness, allergies, pregnancy, poisoning, or "
    "serious symptoms, recommend a veterinarian.\n"
    "Keep answers short, practical, and easy for Indian customers to understand. "
    "Prices are in Indian rupees.\n\n"
    f"Customer question: {user_message}\n\n"
    f"Taffuzo product catalog:\n{catalog_context}"
)

    response = gemini_model.generate_content(
        prompt,
        generation_config={"max_output_tokens": 350}
    )
    answer = response.text.strip()
    return answer, product_suggestions, True


def generate_ai_answer(user_message, products):
    if not gemini_model:
        answer, product_suggestions = fallback_answer(user_message, products)
        return answer, product_suggestions, False

    product_suggestions = find_matching_products(products, user_message)
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
        "made from, answer with the ingredient/details information from the product catalog.\n"
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
        "request_human_agent",
        "human agent",
        "agent",
        "talk to human",
        "talk to agent",
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
        return jsonify({
            "error": "WooCommerce did not return JSON"
        }), 502
    except RequestException as error:
        return jsonify({
            "error": "WooCommerce request failed",
            "details": str(error)
        }), 502
    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 502

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
    """


if __name__ == "__main__":
    app.run(debug=True)
