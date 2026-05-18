import os

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

CK = "ck_0e43ab1bb8ea5984bea7d3a9ff048759e1705698"
CS = "cs_28850237b523c3ad67ea291e8246cd92a09fcf8e"
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv("OPENAI_API_KEY") else None


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

    if matched:
        return "I found a few Taffuzo products that may match your question.", matched

    return (
        "I can help with dog and cat food suggestions. Please tell me your pet's age, "
        "breed size, and whether you want food, treats, or supplements."
    ), []


def generate_ai_answer(user_message, products):
    if not openai_client:
        return fallback_answer(user_message, products)

    product_suggestions = find_matching_products(products, user_message)
    catalog_context = build_catalog_context(products)

    response = openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "developer",
                "content": (
                    "You are Taffuzo ShopBot, a helpful pet food shopping assistant for "
                    "Taffuzo.com. Answer customer questions about dog and cat food, treats, "
                    "feeding choices, and product selection. Use the product catalog when "
                    "recommending products. Keep answers short, friendly, and practical. "
                    "Do not diagnose medical problems. For illness, allergies, pregnancy, "
                    "or serious symptoms, suggest checking with a veterinarian. Prices are "
                    "in Indian rupees."
                )
            },
            {
                "role": "user",
                "content": (
                    f"Customer question: {user_message}\n\n"
                    f"Taffuzo product catalog:\n{catalog_context}"
                )
            }
        ]
    )

    answer = response.output_text.strip()
    return answer, product_suggestions


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
        answer, suggested_products = generate_ai_answer(user_message, products)
    except Exception as error:
        answer, suggested_products = fallback_answer(user_message, products)
        answer = f"{answer} AI answer is temporarily unavailable: {error}"

    return jsonify({
        "answer": answer,
        "products": suggested_products
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
