import os

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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

CK = "ck_0e43ab1bb8ea5984bea7d3a9ff048759e1705698"
CS = "cs_28850237b523c3ad67ea291e8246cd92a09fcf8e"
gemini_api_key = os.getenv("GEMINI_API_KEY")

if gemini_api_key:
    genai.configure(api_key=gemini_api_key)

gemini_model = genai.GenerativeModel(GEMINI_MODEL) if gemini_api_key else None


def format_product(product):
    images = product.get("images") or []
    image = images[0].get("src", "") if images else ""

    return {
        "name": product.get("name", ""),
        "price": product.get("price", ""),
        "image": image,
        "url": product.get("permalink", "")
    }


def fetch_products():
    response = requests.get(
        WC_URL,
        auth=(CK, CS),
        headers={"Accept": "application/json"},
        params={"per_page": 50},
        timeout=10
    )
    response.raise_for_status()
    products = response.json()

    if not isinstance(products, list):
        raise ValueError("WooCommerce returned an unexpected response")

    return products


def find_matching_products(products, user_message):
    words = [
        word
        for word in user_message.lower().replace("-", " ").split()
        if len(word) > 2
    ]
    scored = []

    for product in products:
        name = product.get("name", "").lower()
        description = product.get("short_description", "").lower()
        haystack = f"{name} {description}"
        score = sum(1 for word in words if word in haystack)

        if score:
            scored.append((score, product))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [format_product(product) for _, product in scored[:3]]


def build_catalog_context(products):
    catalog_lines = []

    for product in products[:30]:
        item = format_product(product)
        catalog_lines.append(
            f"- {item['name']} | Price: INR {item['price'] or 'not listed'} | URL: {item['url']}"
        )

    return "\n".join(catalog_lines)


def fallback_answer(user_message, products):
    matched = find_matching_products(products, user_message)
    message = user_message.lower()

    if matched:
        return "I found a few Taffuzo products that may match your question.", matched

    if "cat" in message or "kitten" in message:
        return (
            "For cats, choose food based on age first. Kittens need kitten food with higher "
            "protein and calories, while adult cats need a complete balanced cat food. If "
            "your cat is picky, start with small portions and introduce new food slowly over "
            "7 days. Avoid dog food for cats because cats need taurine and cat-specific "
            "nutrition."
        ), []

    if "puppy" in message or "month" in message or "old" in message:
        return (
            "For a young dog, choose puppy or growth-stage food until adulthood. An "
            "11-month dog may still need puppy food if they are medium or large breed, while "
            "small breeds may be ready to slowly move to adult food. Change food gradually "
            "over 7 days and check with a vet if your dog has allergies, vomiting, or loose stools."
        ), []

    if "treat" in message or "biscuit" in message:
        return (
            "Treats are best used as a small part of the daily diet, usually under 10% of "
            "daily calories. Look for simple ingredients, avoid too many treats in one day, "
            "and pick the right size for your pet."
        ), []

    return (
        "I can help with pet food, treats, and product suggestions. Tell me your pet's age, "
        "breed size, and what you are looking for, and I will suggest a good option."
    ), []


def generate_ai_answer(user_message, products):
    if not gemini_model:
        answer, product_suggestions = fallback_answer(user_message, products)
        return answer, product_suggestions, False

    product_suggestions = find_matching_products(products, user_message)
    catalog_context = build_catalog_context(products)
    prompt = (
        "You are Taffuzo ShopBot, a friendly AI assistant on Taffuzo.com.\n"
        "Answer customer questions naturally and immediately, like a helpful store assistant.\n"
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


@app.route("/chat", methods=["POST"])
def chat():

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Send JSON like {'message': 'shirt'}"}), 400

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
        answer = f"{answer} AI answer is temporarily unavailable: {error}"
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
