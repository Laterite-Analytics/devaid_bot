# devaid.py  (Anvil Server Module, Full‑Python)
import html
import json
import os
import re
from datetime import timedelta, date
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ── DevAid connection ────────────────────────────────────────────────────
API_KEY = os.getenv("DEVAID_API_KEY")
BASE_URL = "https://www.developmentaid.org/api/external"
TIMEOUT = 30

headers = {
    "X-API-KEY": API_KEY,
}

# To obtain the country IDs, run: requests.get(f"{BASE_URL}/dictionaries/locations/global-regions", headers=headers).json()
countries = {
    "Kenya": 35,
    "Rwanda": 51,
    "Ethiopia": 28,
    "Tanzania": 62,
    "Uganda": 65,
    "Sierra Leone": 56,
    "Peru": 109,
}

sectors = {
    "Agriculture": 100,  # Agriculture & Rural Development
    "Education": 5,  # Education, Training & Capacity Building
    "Energy": 6,
    "Environment & NRM": 7,  # Environment & Climate
    "Gender": 9,  # Gender & Human Rights
    "Health": 11,
    "Labour Market & Employment": 14,  # HR & Employment
    # "Micro-finance": 17,
    "Financial Services & Audit": 92,
    "Food systems and Livelihoods": 8,
    "Monitoring & Evaluation": 30,
    "Research & Innovation": 87,
    "Social development": 22,
    "Statistics & Data": 43,
    "Urban development": 34,
    "Water & Sanitation": 48,
    "Youth and Children": 27,
}

# ── Slack connection ────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")

slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None


def slack_post_message(text: str, *, thread_ts: Optional[str] = None) -> Optional[str]:
    """Send a message to Slack using a bot token."""
    if not slack_client:
        print("[WARN] SLACK_BOT_TOKEN not set, message skipped.")
        return None
    if not SLACK_CHANNEL_ID:
        print("[WARN] SLACK_CHANNEL_ID not set, message skipped.")
        return None

    try:
        response = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=text,
            thread_ts=thread_ts,
        )
        return response.get("ts")
    except SlackApiError as e:
        error = getattr(e, "response", {}).get("error") or str(e)
        print(f"[ERROR sending Slack message]: {error}")
    except Exception as e:  # pragma: no cover - defensive guard
        print(f"[ERROR sending Slack message]: {e}")
    return None


def slack_upload_file(
        *,
        file_bytes: bytes,
        filename: str,
        title: Optional[str] = None,
        thread_ts: Optional[str] = None,
):
    """Upload a file to Slack within the tender thread."""
    if not slack_client:
        print(f"[WARN] SLACK_BOT_TOKEN not set, file upload for {filename} skipped.")
        return
    if not SLACK_CHANNEL_ID:
        print(f"[WARN] SLACK_CHANNEL_ID not set, file upload for {filename} skipped.")
        return

    try:
        slack_client.files_upload_v2(
            channel=SLACK_CHANNEL_ID,
            thread_ts=thread_ts,
            filename=filename,
            file=file_bytes,
            title=title or filename,
        )
    except SlackApiError as e:
        error = getattr(e, "response", {}).get("error") or str(e)
        print(f"[ERROR uploading {filename} to Slack]: {error}")
    except Exception as e:  # pragma: no cover - defensive guard
        print(f"[ERROR uploading {filename} to Slack]: {e}")


# ------------------  low‑level helpers  ----------------------------------


def _json_ok(r, *, debug=False):
    if debug:
        print("DEBUG‑HEADERS:", r.status_code, dict(r.headers))
        try:
            print("DEBUG‑BODY   :", r.text[:800])
        except Exception:
            pass
    r.raise_for_status()
    if "application/json" not in r.headers.get("Content-Type", ""):
        raise RuntimeError("Expected JSON, got " + r.headers.get("Content-Type", ""))
    return r.json()


def extract_content_from_answer(answer: str):
    """
    Extracts both the parsed JSON object and accompanying Markdown text
    from an LLM answer.

    Returns:
        (parsed_json: dict or None, markdown_text: str)
    """
    parsed_json = None
    markdown_text = answer.strip()

    try:
        # Try to find JSON inside a fenced code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
            parsed_json = json.loads(json_str)

            # Everything before + after the code block is considered markdown
            start, end = match.span()
            markdown_text = (answer[:start] + answer[end:]).strip()

        else:
            # Fallback: look for the first JSON-like block
            match = re.search(r"(\{.*\})", answer, re.DOTALL)
            if match:
                json_str = match.group(1).strip()
                parsed_json = json.loads(json_str)

                # Remove JSON from the Markdown text
                markdown_text = re.sub(r"(\{.*\})", "", answer, flags=re.DOTALL).strip()

    except json.JSONDecodeError as e:
        print(f"[Error] Invalid JSON: {e}")
    except Exception as e:
        print(f"[Error extracting JSON]: {e}")

    return parsed_json, markdown_text


def fetch_tender_details(tender_id):
    response = requests.get(
        f"{BASE_URL}/tenders/{tender_id}", headers=headers, timeout=TIMEOUT
    )
    tender = _json_ok(response)
    print(f"  ↳ Donor: {', '.join(d['name'] for d in tender['donors'])}")
    print(f"  ↳ URL: {tender['url']}")
    return tender


def get_document_for_tender(tender_id, document_info):
    document_id = document_info.get("id")
    if not document_id:
        raise ValueError("Document entry missing 'id'.")
    response = requests.get(
        f"{BASE_URL}/tenders/{tender_id}/documents/{document_id}",
        headers=headers,
        timeout=TIMEOUT,
    )

    filename = (
            document_info.get("fileName")
            or document_info.get("name")
            or f"tender-{tender_id}-{document_id}"
    )
    document_bytes = response.content

    return {
        "filename": filename,
        "data": document_bytes,
    }


def find_tender_requirements(tender_url: str):
    """
    Use an LLM with web search capability to find and summarize
    tender submission requirements from the organization website.

    The LLM explores related pages (starting from tender_url),
    extracts the requirements, and returns:
      - a summary of requirements
      - the actual link(s) where they were found.
    """
    query = f"""
        You are an expert in public tenders. Starting from the tender page {tender_url},
        search the organization's website and any related sources to identify **submission requirements** —
        including eligibility criteria, required documentation, technical and financial qualifications, timelines, and deadlines.
        
        Summarize your findings in concise bullet points.
        
        At the end, provide the most relevant and authoritative link(s)
        where these requirements can be verified, formatted as Slack links in the form `<{{url}}|display name>`.
        
        Format your response as:
        
        Requirements:
        - <requirement 1>
        - <requirement 2>
        ...
        
        Source(s):
        <https://example.com|Organization Website>
        """
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search"}],
        input=query,
        timeout=TIMEOUT,
    )
    output_text = response.output_text

    return output_text


def simple_go_no_go_analysis(tender_info):
    """
    Automated Go/No-Go analysis aligned with Laterite's BD decision framework,
    refined using phrasing and risk logic from the LateriteAI prompt.
    """
    query = f"""
    You are an expert analyst on *Laterite’s Business Development (BD) team*.
    
    Laterite operates across *Rwanda, Ethiopia, Tanzania, Uganda, Kenya, Sierra Leone, Peru*,
    and occasionally *the Netherlands* (for non-survey work).
    
    Your task is to assess whether Laterite should bid on the following opportunity.
    
    Tender information:
    {str(tender_info)[:10000]}  # truncate to fit token limits
    
    ---------------------------------------------------------
    🎯 OBJECTIVE
    ---------------------------------------------------------
    Make a structured analysis that mimics the decision process used by Laterite’s BD team.
    You must balance *strategic alignment, feasibility, and risk awareness*.
    
    Use sound judgment and a conservative approach — if critical information is missing,
    mark the corresponding criterion as “Maybe (0.5)” or “No (0)”.
    
    ---------------------------------------------------------
    🧩 DECISION FRAMEWORK
    ---------------------------------------------------------
    Rank each of the following criteria using the scoring system:
    • Yes = 1
    • Maybe = 0.5
    • No = 0
    
    For each, provide a one-sentence rationale, to be included in the final rationale within the json output.
    
    1️⃣ **Thematic Area Fit**
       - Does the opportunity align with Laterite’s research areas (impact evaluations, data systems, surveys, monitoring & evaluation)?
       - Is it feasible for the country teams involved?
    
    2️⃣ **Available Expertise**
       - Does Laterite have the in-house skills and experience required?
       - Would we need to partner to be competitive?
    
    3️⃣ **Strategic Alignment**
       - Is the project strategic for Laterite (e.g., high-value client, strengthens our portfolio, builds credibility in a growth sector)?
       - Does it fit our medium-term BD priorities?
    
    4️⃣ **Budget & Timeline Realism**
       - Is the budget realistic given the expected scope of work?
       - Is the timeline feasible for a strong submission?
       - Flag red flags (e.g., budget <150k USD, local currency budgets, very short deadlines).
    
    5️⃣ **Application Process / LOE**
       - Do we have sufficient time and internal capacity to prepare the proposal or EOI?
       - Note any red flags: pay-to-apply, hard-copy submissions, or unclear requirements.
    
    ---------------------------------------------------------
    ⚖️ RISK-INFORMED DECISIONING
    ---------------------------------------------------------
    You must weigh:
    (a) Fit (sector, methods, geography),
    (b) Eligibility/Compliance (e.g., registration, past performance),
    (c) Budget & timeline realism, and
    (d) Risk level.
    
    Guidelines:
    • If Eligibility = No or Risk = High with no credible mitigation → recommend *NO-GO*.
    • If opportunity is strong but depends on solvable issues (e.g., needing a partner, clarifying scope) → recommend *GO (conditional)*.
    • Otherwise, recommend *GO*.
    
    ---------------------------------------------------------
    💡 SCORING & DECISION LOGIC
    ---------------------------------------------------------
    Compute:
    - total_score = sum of all five criteria (max = 5).
    - Map to decisions:
      • total_score ≥ 4.5 → "GO"
      • 3.5 ≤ total_score < 4.5 → "GO (conditional)"
      • total_score < 3.5 → "NO-GO"
    
    Confidence:
    - Derive from how consistent and well-supported the evidence is.
    - High confidence (≥0.85) if information is clear and aligns strongly with Laterite’s profile.
    - Medium (0.6–0.8) if partial or uncertain data.
    - Low (<0.6) if key information missing.
    
    ---------------------------------------------------------
    📄 OUTPUT FORMAT
    ---------------------------------------------------------
    Return a **single JSON object**, with this structure:
    
    {{
      "decision": "GO (conditional)",  # GO | GO (conditional) | NO-GO
      "confidence": 0.82,
      "rationale": "The opportunity fits Laterite’s methods and sectors but budget and timeline are tight.",
      "scores": {{
        "thematic_area_fit": 1,
        "available_expertise": 1,
        "strategic_alignment": 0.5,
        "budget_timeline_realism": 0.5,
        "application_process": 0.5
      }},
      "total_score": 3.5,
      "key_criteria": {{
        "geographic_fit": "Yes",
        "sector_fit": "Yes",
        "eligibility": "Partial",
        "risk_level": "Medium"
      }}
    }}
    Include a short markdown text in addition to the JSON object, it will be parsed programmatically.
    
    Notes:
    • Be explicit in rationale about any uncertainties or assumptions.
    • Use online search to assess donor/organization reputation.
    • Be concise but clear — this output feeds directly into Slack.
    """
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search"}],
        input=query,
    )
    return extract_content_from_answer(response.output_text)


# ------------------  message formatting  ----------------------------------


def format_tender_description_for_slack(tender_info):
    """
    Format a tender detail into a professional Slack message
    for the BDC team, when responding to a request for more info.
    """
    title = tender_info.get("name", "Untitled Tender")
    url = tender_info.get("url", "")
    deadline = tender_info.get("deadline", "N/A")
    posted = tender_info.get("postedDate", "N/A")
    status = tender_info.get("status", "unknown").capitalize()

    organization = tender_info.get("organization", {}).get("name", "Unknown organization")
    donor = ", ".join([d.get("name", "") for d in tender_info.get("donors", [])]) or "N/A"
    country = (
            ", ".join([loc.get("name", "") for loc in tender_info.get("locations", [])])
            or "Unspecified"
    )
    sector = (
            ", ".join([s.get("name", "") for s in tender_info.get("sectors", [])])
            or "Unspecified"
    )

    # Clean up and simplify the description
    raw_description = tender_info.get("description", "")
    soup = BeautifulSoup(raw_description, "html.parser")
    text = soup.get_text()
    description = html.unescape(text)
    description = re.sub(r"\n{2,}", "\n", description).strip()

    amount = tender_info.get("amount", {})
    budget = amount.get("value")
    currency = amount.get("currency")
    budget_str = f"{budget:,} {currency}" if budget and currency else "Not specified"

    # Contact info
    contact_email = tender_info.get("email") or tender_info.get("contactEmail") or ""
    contacts = tender_info.get("contacts", [])
    contact_lines = []
    for c in contacts:
        name = c.get("name", "")
        mail = c.get("mainEmail", "")
        if name or mail:
            contact_lines.append(f"{name} ({mail})" if name else mail)
    contact_text = ", ".join(contact_lines) or contact_email or "N/A"

    slack_core_message = (
        f"*Tender Details — {title}*\n"
        f"──────────────────────────────\n"
        f"• 🏢 *Organization:* {organization}\n"
        f"• 🌍 *Country:* {country}\n"
        f"• 🎯 *Sector:* {sector}\n"
        f"• 💰 *Budget:* {budget_str}\n"
        f"• 🤝 *Donor:* {donor}\n"
        f"• 📅 *Posted on:* {posted}\n"
        f"• ⏰ *Deadline:* {deadline}\n"
        f"• 🚦 *Status:* {status}\n"
        f"\n──────────────────────────────\n"
        f"📧 *Contact:* {contact_text}\n")
    # Add URL and footer
    if url:
        slack_core_message += f"🔗 *More info:* <{url}|Open Tender Page>\n"
    slack_core_message += f"_Provided by the BDC Tender Fetcher Bot — {date.today():%d %b %Y}_ 🤖"

    slack_summary = f"*Summary:*\n{description[:5000]}{'...' if len(description) > 5000 else ''}\n\n"

    requirements_summary = tender_info.get("requirements_summary", "No specific requirements found.")
    slack_requirements = f"*Application Requirements:*\n{requirements_summary}\n"

    slack_gonogo_message = ""
    go_no_go = tender_info.get("go_no_go_analysis")
    go_no_go_text = go_no_go["text"] if go_no_go else ""
    go_no_go_json = go_no_go["analysis_json"] if go_no_go else ""
    if go_no_go_json:
        decision = go_no_go.get("decision", "N/A").upper()
        confidence = go_no_go.get("confidence")
        confidence_pct = f"{confidence * 100:.0f}%" if isinstance(confidence, (int, float)) else "N/A"
        rationale = go_no_go.get("rationale", "No rationale provided.")
        criteria = go_no_go.get("key_criteria", {})
        scores = go_no_go.get("scores", {})
        total_score = go_no_go.get("total_score")

        # Emoji map for decision
        emoji_map = {
            "GO": "✅",
            "GO (CONDITIONAL)": "⚠️",
            "NO-GO": "❌",
        }
        emoji = emoji_map.get(decision, "❓")

        # Emoji map for scores
        def score_emoji(value):
            if value == 1:
                return ":large_green_circle:"
            elif value == 0.5:
                return ":large_yellow_circle:"
            elif value == 0:
                return ":red_circle:"
            else:
                return ":white_circle:"

        slack_gonogo_message += (
            f"📊 *Go/No-Go Analysis*\n"
            f"• *Decision:* {emoji} {decision}\n"
            f"• *Confidence:* {confidence_pct}\n"
            f"• *Rationale:* {rationale}\n"
        )

        # Add detailed scoring breakdown if available
        if scores:
            slack_gonogo_message += "• *Detailed Scores:*\n"
            for key, val in scores.items():
                label = key.replace("_", " ").capitalize()
                slack_gonogo_message += f"   • {score_emoji(val)} {label}: {val}\n"

        if total_score is not None:
            slack_gonogo_message += f"• *Total Score:* *{total_score:.1f} / 5.0*\n"

        # Add key criteria (fit, eligibility, risk)
        if criteria:
            slack_gonogo_message += "• *Key Criteria:*\n"
            for key, value in criteria.items():
                label = key.replace("_", " ").capitalize()
                slack_gonogo_message += f"   • {label}: {value}\n"

        slack_gonogo_message += go_no_go_text

    return slack_core_message, slack_summary, slack_requirements, slack_gonogo_message


# ── Main task ------------------------------------------------------


def fetch_new_tenders(page_size=50):
    today = date.today()
    weekday = today.weekday()  # Monday=0, Sunday=6
    if weekday == 0:
        # Monday → get posts since Friday
        previous_working_day = today - timedelta(days=3)
    else:
        # Other weekdays → get posts since yesterday
        previous_working_day = today - timedelta(days=1)

    body = {
        "sort": "posted_date.desc",
        "page": 1,
        "size": page_size,
        "filter": {
            "keyword": {"searchedText": "survey | research | evaluation | monitoring",
                        "searchedFields": ["title", "description", "documents"]},
            "locations": list(countries.values()),
            "sectors": list(sectors.values()),
            "postedFrom": str(previous_working_day),
            "postedTill": str(today),
            "statuses": [
                2,
                3,
                8,
                9,
                10,
            ],
            # :[{"id":8,"name":"country programming","stage":{"id":"early_intelligence","name":"Early intelligence"}},{"id":9,"name":"formulation","stage":{"id":"early_intelligence","name":"Early intelligence"}},{"id":10,"name":"approval","stage":{"id":"early_intelligence","name":"Early intelligence"}},{"id":2,"name":"forecast","stage":{"id":"procurement","name":"Procurement"}},{"id":3,"name":"open","stage":{"id":"procurement","name":"Procurement"}},{"id":4,"name":"closed","stage":{"id":"procurement","name":"Procurement"}},{"id":5,"name":"shortlisted","stage":{"id":"procurement","name":"Procurement"}},{"id":6,"name":"awarded","stage":{"id":"procurement","name":"Procurement"}},{"id":7,"name":"cancelled","stage":{"id":"procurement","name":"Procurement"}},{"id":11,"name":"completion and evaluation","stage":{"id":"implementation","name":"Implementation"}}]
            "tenderTypes": [4],  # consulting services
            "eligibilityAlias": "organisation",
            "budgetInEuroRange": {
                "min": 15000,
                "max": 20000000,
            },  # 15k to 20M EUR, 20M is the max allowed
            # "locationIsStrict": false,
            # "sectorsIsStrict": false,
            # "typesIsStrict": false,
        },
    }
    try:
        # Fetch tenders
        response = requests.post(
            f"{BASE_URL}/tenders/search", headers=headers, json=body
        )
        tenders = _json_ok(response).get("items", [])
        print(f"Fetched {len(tenders)} new tenders from DevAid.")
    except requests.HTTPError as e:
        tenders = []
        print(f"[ERROR] {e}")

    return [tenders[i]["id"] for i in range(len(tenders))]


def fetch_multiple_tenders_details(tender_ids: List[str]):
    tender_details = {}
    for tender_id in tender_ids:
        # --------- TENDER INFORMATION COLLECTION ---------
        try:
            # General info for that tender
            info = fetch_tender_details(tender_id)
            # Tenders application requirements using LLM
            requirements = find_tender_requirements(info.get("url", ""))
            info["requirements_summary"] = requirements if requirements else {}
            # Simple GO/NO-GO analysis
            analysis_json, analysis_text = simple_go_no_go_analysis(info)
            info["go_no_go_analysis"] = {"analysis_json": analysis_json, "text": analysis_text}
            # PDF Documents for that tender
            for document_info in info.get("documents", []):
                try:
                    document = get_document_for_tender(tender_id, document_info)
                except Exception as e:
                    print(f"  [ERROR fetching document {document_info.get('id')} for {tender_id}: {e}]")
                    continue
                if document:
                    info.setdefault("document_details", []).append(document)
                else:
                    print(f"  [No document found for ID {document_info.get('id')}]")
        except Exception as e:
            print(f"  [ERROR fetching details for {tender_id}: {e}]")
            continue

        tender_details[tender_id] = info

        # --------- SLACK MESSAGE SENDING ---------
        slack_core_message, slack_summary, slack_requirements, slack_go_no_go = format_tender_description_for_slack(
            info)
        print(slack_core_message)
        # try:
        #     core_ts = slack_post_message(slack_core_message)
        #     slack_post_message(slack_summary, thread_ts=core_ts)
        #     slack_post_message(slack_requirements, thread_ts=core_ts)
        #     slack_post_message(slack_go_no_go, thread_ts=core_ts)
        # except Exception as e:
        #     print(f"  [ERROR sending Slack message for {tender_id}: {e}]")
        #     continue
        #
        # try:
        #     for document in info.get("document_details", []):
        #         data = document.get("data")
        #         filename = document.get("filename")
        #         slack_upload_file(
        #             file_bytes=data,
        #             filename=filename,
        #             title=filename,
        #             thread_ts=core_ts
        #         )
        # except Exception as e:
        #     print(f"  [ERROR uploading document to Slack for {tender_id}: {e}]")
        #     continue

    return tender_details


if __name__ == "__main__":
    new_tender_ids = fetch_new_tenders()
    print(f"new_tender_ids: {new_tender_ids}")
    fetch_multiple_tenders_details(new_tender_ids)
