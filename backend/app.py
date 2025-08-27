import os
import time
import uuid
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
from pymongo import MongoClient
from PIL import Image
import pytesseract
from datetime import datetime
from bson import ObjectId



# -------------------- Flask / SocketIO / DB --------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "secret!")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(MONGO_URI)
db = client["swapit"]
tickets = db.tickets
negos = db.negotiations
messages = db.messages

# -------------------- Ollama config --------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

def ask_ollama(prompt: str) -> str:
    """Send prompt to Ollama API and return the response."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print("Ollama error:", e)
        return "Sorry, I could not process that request."

# -------------------- Helpers --------------------
def utcnow():
    return int(time.time())

def room_name(negotiation_id: str) -> str:
    return f"neg-{negotiation_id}"

def store_message(negotiation_id: str, role: str, text: str):
    msg = {"negotiationId": negotiation_id, "role": role, "text": text, "ts": utcnow()}
    messages.insert_one(msg)

# -------------------- REST: Ticket OCR --------------------
@app.route("/process_ticket", methods=["POST"])
def process_ticket():

    
    try:
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "No valid file uploaded"}), 400

        ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png").lower()
        temp_path = f"temp_upload.{ext}"
        file.save(temp_path)

        image = Image.open(temp_path)
        extracted_text = pytesseract.image_to_string(image)
        os.remove(temp_path)

        prompt = (
    "You are a ticket parsing assistant.\n\n"
    "Extract the following structured details from the OCR text of a travel ticket:\n"
    "- ticketId\n"
    "- pnr\n"
    "- from (origin)\n"
    "- to (destination)\n"
    "- date (departure date)\n"
    "- arrivalDate\n"
    "- time (departure time)\n"
    "- arrivalTime\n"
    "- seat\n"
    "- busType\n"
    "- price (include ? symbol)\n"
    "- passengerName\n"
    "- age\n"
    "- travelCompany\n\n"
    "Return the result strictly as a JSON object with these exact keys. "
    "If any field is missing or unclear, set its value to `null`.\n\n"
    "Ensure the price field includes the ? symbol before the numeric value. "
    "Clean the text to remove extra whitespace or non-printable characters.\n\n"
    "Here is the OCR text:\n\n"
    + extracted_text
)
        ollama_output = ask_ollama(prompt)

        ticket_doc = {
            "ocr_text": extracted_text.strip(),
            "ollama_summary": ollama_output.strip(),
            "created_at": utcnow(),
        }
        result = tickets.insert_one(ticket_doc)

        return jsonify({
            "ticket_id": str(result.inserted_id),
            "ocr_text": extracted_text.strip(),
            "ollama_summary": ollama_output.strip(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------------------- REST: Ticket CRUD --------------------
@app.route("/upload_ticket", methods=["POST"])
def upload_ticket():
    data = request.json
    ticket = {
        "ticketId": str(uuid.uuid4()),
        "sellerId": data.get("sellerId"),
        "category": data.get("category"),
        "details": data.get("details", {}),
        "askPrice": data.get("details", {}).get("price"),
        "status": "available",
        "createdAt": utcnow(),
    }
    tickets.insert_one(ticket)
    return jsonify({"message": "Ticket saved", "ticket": ticket}), 201

@app.route("/tickets", methods=["GET"])
def list_tickets():
    category = request.args.get("category")
    query = {"category": category} if category else {}
    return jsonify(list(tickets.find(query, {"_id": 0})))

@app.route("/start_negotiation", methods=["POST"])
def start_negotiation():
    data = request.json
    ticket_id = data["ticketId"]
    buyer_id = data["buyerId"]

    t = tickets.find_one({"ticketId": ticket_id})
    if not t:
        return jsonify({"error": "Ticket not found"}), 404
    if t.get("status") != "available":
        return jsonify({"error": "Ticket not available"}), 400

    nego = {
        "negotiationId": str(uuid.uuid4()),
        "ticketId": ticket_id,
        "sellerId": t["sellerId"],
        "buyerId": buyer_id,
        "status": "open",
        "agreedPrice": None,
        "createdAt": utcnow(),
        "lastUpdate": utcnow(),
    }
    negos.insert_one(nego)
    return jsonify({"negotiation": {k: v for k, v in nego.items() if k != "_id"}}), 201
# --------- Questions for User ---------

@app.route("/generate-questions", methods=["GET"])
def generate_questions():
    category = request.args.get("category", "general")

    prompt = f"""
    Generate exactly 10 seller questions for a {category} ticket listing.
    Respond ONLY in valid JSON like this:
    [
      {{"id": 1, "question": "What is the departure city?"}},
      {{"id": 2, "question": "What is the destination city?"}}
    ]
    """

    result = subprocess.run(
        ["ollama", "run", "llama3", prompt],
        capture_output=True, text=True
    )

    output = result.stdout.strip()

    try:
        # Sometimes model adds text before/after JSON → try to extract
        json_str = output[output.find("["):output.rfind("]")+1]
        questions = json.loads(json_str)
    except Exception as e:
        print("Parsing error:", e, output)
        questions = [{"id": 0, "question": "Failed to generate questions"}]

    return jsonify(questions)


# -------------------- Socket.IO --------------------
session_index = {}

@socketio.on("connect")
def on_connect():
    print("Client connected")
    emit("system", "Connected to Swapit mediator.")

@socketio.on("disconnect")
def on_disconnect():
    print("Client disconnected")

@socketio.on("join_as_seller")
def join_as_seller(data):
    negotiation_id = data["negotiationId"]
    seller_id = data["sellerId"]
    nego = negos.find_one({"negotiationId": negotiation_id})
    if not nego or nego["sellerId"] != seller_id:
        emit("error", "Negotiation not found or seller mismatch.")
        return

    join_room(room_name(negotiation_id))
    session_index.setdefault(negotiation_id, {})["seller_sid"] = request.sid
    emit("system", "Seller joined negotiation.", to=request.sid)

@socketio.on("join_as_buyer")
def join_as_buyer(data):
    negotiation_id = data["negotiationId"]
    buyer_id = data["buyerId"]
    nego = negos.find_one({"negotiationId": negotiation_id})
    if not nego or nego["buyerId"] != buyer_id:
        emit("error", "Negotiation not found or buyer mismatch.")
        return

    join_room(room_name(negotiation_id))
    session_index.setdefault(negotiation_id, {})["buyer_sid"] = request.sid
    emit("system", "Buyer joined negotiation.", to=request.sid)

# -------------------- Mediation Messages --------------------
@socketio.on("buyer_to_agent")
def buyer_to_agent(data):
    negotiation_id = data["negotiationId"]
    buyer_id = data["buyerId"]
    text = data.get("message", "")
    budget = data.get("budget")

    nego = negos.find_one({"negotiationId": negotiation_id})
    if not nego or nego["buyerId"] != buyer_id:
        emit("error", "Negotiation not found or buyer mismatch.")
        return

    store_message(negotiation_id, "buyer", text)

    ticket = tickets.find_one({"ticketId": nego["ticketId"]}) or {}
    ask_price = ticket.get("askPrice")
    prompt = f"""
You are Swapit's AI mediator. Summarize buyer intent and propose a next step for the SELLER.
Ticket details: {json.dumps(ticket.get("details", {}), ensure_ascii=False)}
Seller ask price: {ask_price}
Buyer message: "{text}"
Buyer budget: {budget}
"""
    agent_to_seller = ask_ollama(prompt)
    store_message(negotiation_id, "agent", f"(to seller) {agent_to_seller}")

    seller_sid = session_index.get(negotiation_id, {}).get("seller_sid")
    if seller_sid:
        socketio.emit("agent_to_seller", {"message": agent_to_seller}, to=seller_sid)
    emit("agent_ack", {"message": "Noted. I’m checking with the seller now."})

@socketio.on("seller_to_agent")
def seller_to_agent(data):
    negotiation_id = data["negotiationId"]
    seller_id = data["sellerId"]
    text = data.get("message", "")
    min_accept = data.get("minAcceptable")

    nego = negos.find_one({"negotiationId": negotiation_id})
    if not nego or nego["sellerId"] != seller_id:
        emit("error", "Negotiation not found or seller mismatch.")
        return

    store_message(negotiation_id, "seller", text)

    ticket = tickets.find_one({"ticketId": nego["ticketId"]}) or {}
    ask_price = ticket.get("askPrice")
    prompt = f"""
You are Swapit's AI mediator. Convert SELLER response into a buyer-facing message.
Ticket details: {json.dumps(ticket.get("details", {}), ensure_ascii=False)}
Seller says: "{text}"
Seller minimum acceptable: {min_accept}
"""
    agent_to_buyer = ask_ollama(prompt)
    store_message(negotiation_id, "agent", f"(to buyer) {agent_to_buyer}")

    buyer_sid = session_index.get(negotiation_id, {}).get("buyer_sid")
    if buyer_sid:
        socketio.emit("agent_to_buyer", {"message": agent_to_buyer}, to=buyer_sid)
    emit("agent_ack", {"message": "Thanks. I’m relaying this to the buyer."})

# -------------------- Run --------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
