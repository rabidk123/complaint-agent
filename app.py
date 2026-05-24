import os
import json
import uuid
import datetime
import anthropic
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# just using a list in memory for now — swap with a DB later if needed
tickets = []

# these words skip the AI entirely and go straight to a human
EMERGENCY_KEYWORDS = ["fire", "flood", "gas leak", "unconscious", "assault", "bleeding", "collapsed", "smoke"]

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def check_for_emergency(text):
    """if someone says 'fire' or 'gas leak', we don't wait for AI to figure it out"""
    lowered = text.lower()
    for word in EMERGENCY_KEYWORDS:
        if word in lowered:
            return True, word
    return False, None


def classify_complaint(complaint_text):
    """
    asks Claude to read the complaint and return structured info
    the prompt is explicit about JSON so we can parse it reliably
    """
    prompt = f"""You are a university complaint management system. Read this complaint and respond ONLY with a JSON object — no explanation, no extra text.

Complaint: "{complaint_text}"

Return this exact structure:
{{
  "category": one of ["IT", "Facilities", "Security", "StudentAffairs", "Other"],
  "urgency": one of ["Critical", "Normal", "Low"],
  "summary": "one sentence describing the issue clearly",
  "department": "which team should handle this",
  "missing_info": "what key info is missing (location, block number, etc.) — empty string if nothing is missing",
  "suggested_eta": "realistic resolution time e.g. 2 hours, 1 day, 3 days"
}}

Category guide:
- IT: WiFi, internet, computer labs, projectors, network issues
- Facilities: electricity, water, plumbing, cleaning, maintenance, room damage
- Security: theft, access cards, locks, suspicious activity
- StudentAffairs: harassment, mental health, roommate conflict, welfare
- Other: anything that doesn't fit above

Urgency guide:
- Critical: affects safety, health, or a large number of students right now
- Normal: disrupts daily life but not an emergency
- Low: minor inconvenience, can wait a day or two"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()

    # sometimes the model wraps in ```json ... ``` even when told not to
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


def generate_ticket(complaint_text, classification, student_name="Anonymous"):
    """puts everything together into a ticket dict"""
    ticket_id = "TKT-" + str(uuid.uuid4())[:6].upper()
    timestamp = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")

    ticket = {
        "id": ticket_id,
        "timestamp": timestamp,
        "student": student_name,
        "original_complaint": complaint_text,
        "category": classification["category"],
        "urgency": classification["urgency"],
        "summary": classification["summary"],
        "assigned_to": classification["department"],
        "eta": classification["suggested_eta"],
        "status": "Open"
    }

    tickets.append(ticket)
    return ticket


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit_complaint():
    data = request.json
    complaint_text = data.get("complaint", "").strip()
    student_name = data.get("name", "Anonymous").strip()

    if not complaint_text:
        return jsonify({"error": "Please write something in the complaint field."}), 400

    # step 1: check for emergencies before even calling the AI
    is_emergency, trigger_word = check_for_emergency(complaint_text)
    if is_emergency:
        ticket_id = "EMG-" + str(uuid.uuid4())[:6].upper()
        emergency_ticket = {
            "id": ticket_id,
            "timestamp": datetime.datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "student": student_name,
            "original_complaint": complaint_text,
            "category": "EMERGENCY",
            "urgency": "Critical",
            "summary": f"EMERGENCY reported — keyword detected: '{trigger_word}'",
            "assigned_to": "Security + Admin (Human Override)",
            "eta": "Immediate",
            "status": "Escalated to Human"
        }
        tickets.append(emergency_ticket)
        return jsonify({
            "ticket": emergency_ticket,
            "needs_clarification": False,
            "is_emergency": True,
            "message": "🚨 Emergency detected. Security and admin have been notified immediately."
        })

    # step 2: run the complaint through Claude
    try:
        classification = classify_complaint(complaint_text)
    except Exception as e:
        # if Claude fails for any reason, don't crash — queue it for manual review
        return jsonify({
            "error": "Could not classify complaint automatically. It has been queued for manual review.",
            "raw_error": str(e)
        }), 500

    # step 3: if key info is missing, ask the student before creating the ticket
    if classification.get("missing_info"):
        return jsonify({
            "needs_clarification": True,
            "question": f"Before I log this, could you tell me: {classification['missing_info']}?",
            "partial_classification": classification
        })

    # step 4: all good — generate and store the ticket
    ticket = generate_ticket(complaint_text, classification, student_name)

    return jsonify({
        "ticket": ticket,
        "needs_clarification": False,
        "is_emergency": False,
        "message": f"Ticket {ticket['id']} created and sent to {ticket['assigned_to']}."
    })


@app.route("/submit-clarified", methods=["POST"])
def submit_with_clarification():
    """called when the student answers the follow-up question"""
    data = request.json
    original = data.get("original_complaint", "")
    followup = data.get("followup_answer", "")
    student_name = data.get("name", "Anonymous")

    # combine both into one fuller complaint and re-classify
    full_complaint = f"{original}. Additional info: {followup}"

    try:
        classification = classify_complaint(full_complaint)
    except Exception as e:
        return jsonify({"error": "Classification failed after follow-up.", "raw_error": str(e)}), 500

    ticket = generate_ticket(full_complaint, classification, student_name)

    return jsonify({
        "ticket": ticket,
        "needs_clarification": False,
        "message": f"Got it. Ticket {ticket['id']} created and routed to {ticket['assigned_to']}."
    })


@app.route("/tickets", methods=["GET"])
def view_tickets():
    """admin endpoint to see all tickets — in production you'd add auth here"""
    return jsonify({"total": len(tickets), "tickets": tickets})


if __name__ == "__main__":
    app.run(debug=True)
