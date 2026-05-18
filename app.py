from flask import Flask, request, jsonify
from flask_cors import CORS
from requests.exceptions import JSONDecodeError, RequestException
import requests

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

CK = "ck_0e43ab1bb8ea5984bea7d3a9ff048759e1705698"
CS = "cs_28850237b523c3ad67ea291e8246cd92a09fcf8e"


@app.route("/chat", methods=["POST"])
def chat():

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").lower()

    if not user_message:
        return jsonify({"error": "Send JSON like {'message': 'shirt'}"}), 400

    try:
        response = requests.get(
            WC_URL,
            auth=(CK, CS),
            headers={"Accept": "application/json"},
            timeout=10
        )
        response.raise_for_status()
        products = response.json()
    except JSONDecodeError:
        return jsonify({
            "error": "WooCommerce did not return JSON",
            "status_code": response.status_code,
            "response_preview": response.text[:200]
        }), 502
    except RequestException as error:
        return jsonify({
            "error": "WooCommerce request failed",
            "details": str(error)
        }), 502

    if not isinstance(products, list):
        return jsonify({
            "error": "WooCommerce returned an unexpected response",
            "response": products
        }), 502

    matched = []

    for p in products:

        if user_message in p.get("name", "").lower():

            image = ""

            if len(p.get("images", [])) > 0:
                image = p["images"][0].get("src", "")

            matched.append({
                "name": p.get("name", ""),
                "price": p.get("price", ""),
                "image": image,
                "url": p.get("permalink", "")
            })

    return jsonify(matched[:3])


@app.route("/")
def home():
    return """
    <h1>Shopbot is running</h1>
    <p>Send a POST request to <code>/chat</code> with JSON:</p>
    <pre>{"message": "shirt"}</pre>
    """


if __name__ == "__main__":
    app.run(debug=True)
