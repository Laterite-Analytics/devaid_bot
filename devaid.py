# devaid.py  (Anvil Server Module, Full‑Python)
import html
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
    query = (
        f"You are an expert in public tenders. Starting from the tender page {tender_url}, "
        f"search the organization’s website and any related sources to find **submission requirements** — "
        f"including eligibility criteria, required documentation, technical and financial qualifications, timeline and deadlines. "
        f"Summarize them in bullet points. "
        f"At the end, provide the most relevant and authoritative link(s) "
        f"where these requirements can be verified. "
        f"Format the response as:\n\n"
        f"Requirements:\n- <requirement 1>\n- <requirement 2>\n...\n\n"
        f"Source(s): <one or more URLs>"
    )
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search"}],
        input=query,
        timeout=TIMEOUT,
    )
    output_text = response.output_text

    return output_text


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

        f"──────────────────────────────\n"
        f"📧 *Contact:* {contact_text}\n"
    )
    if url:
        slack_core_message += f"🔗 *More info:* <{url}|Open Tender Page>\n"
    slack_core_message += (
        f"_Provided by the BDC Tender Fetcher Bot — {date.today():%d %b %Y}_ 🤖"
    )

    slack_summary = f"*Summary:*\n{description[:5000]}{'...' if len(description) > 5000 else ''}\n\n"

    requirements_summary = tender_info.get("requirements_summary", "No specific requirements found.")
    slack_requirements = f"*Application Requirements:*\n{requirements_summary}\n"

    return slack_core_message, slack_summary, slack_requirements


# ── Background task ------------------------------------------------------


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


def fetch_multiple_tenders_details(tender_ids: List[str], *, thread_ts: Optional[str] = None):
    tender_details = {}
    for tender_id in tender_ids:
        # --------- TENDER INFORMATION COLLECTION ---------
        try:
            # General info for that tender
            info = fetch_tender_details(tender_id)
            # Tenders application requirements using LLM
            requirements = find_tender_requirements(info.get("url", ""))
            info["requirements_summary"] = requirements if requirements else {}
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
        slack_core_message, slack_summary, slack_requirements = format_tender_description_for_slack(info)
        try:
            core_ts = slack_post_message(slack_core_message)
            slack_post_message(slack_summary, thread_ts=core_ts)
            slack_post_message(slack_requirements, thread_ts=core_ts)
        except Exception as e:
            print(f"  [ERROR sending Slack message for {tender_id}: {e}]")
            continue

        try:
            for document in info.get("document_details", []):
                data = document.get("data")
                filename = document.get("filename")
                slack_upload_file(
                    file_bytes=data,
                    filename=filename,
                    title=filename,
                )
        except Exception as e:
            print(f"  [ERROR uploading document to Slack for {tender_id}: {e}]")
            continue

    return tender_details


if __name__ == "__main__":
    new_tender_ids = fetch_new_tenders()
    print(f"new_tender_ids: {new_tender_ids}")
    fetch_multiple_tenders_details(new_tender_ids)
