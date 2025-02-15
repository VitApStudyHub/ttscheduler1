import streamlit as st
import re
import datetime
import pandas as pd
import os
import pickle
import csv
from io import StringIO
from datetime import datetime, timedelta

# Instead of run_local_server, we'll do a manual code flow:
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from ics import Calendar, Event  # your usage for ICS if you need it

# ---------------- Disable Enter Key ----------------
st.markdown(
    """
    <script>
    document.addEventListener('keydown', function(event) {
      // Prevent default action if Enter is pressed
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    </script>
    """,
    unsafe_allow_html=True
)

# ---------------- Google Calendar Setup ----------------
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def authenticate_google_calendar_manual():
    """
    Manual OAuth flow that automatically opens the Google sign-in page in a new tab,
    then asks the user to paste the code back into the Streamlit app.
    """
    creds = None

    # 1) Check if we already have credentials in session_state
    if "google_token" in st.session_state:
        creds = pickle.loads(st.session_state["google_token"])

    # 2) If no valid creds, do a manual code flow
    if not creds or not creds.valid:
        # If we have partial creds but they're expired, try to refresh
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state["google_token"] = pickle.dumps(creds)
                return build("calendar", "v3", credentials=creds)
            except Exception as e:
                st.error(f"Failed to refresh token: {e}")
                return None
        else:
            # Must start a brand-new flow
            if not os.path.exists("credentials.json"):
                st.error("No credentials.json found! Please place it in the same directory.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)

            # If we haven't generated the auth_url yet, do so now
            if "auth_url" not in st.session_state:
                auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
                st.session_state["auth_url"] = auth_url

                # Attempt to auto-open the link in a new tab:
                open_script = f"""
                <script>
                    window.open("{auth_url}", "_blank");
                </script>
                """
                st.markdown(open_script, unsafe_allow_html=True)

            # 3) Ask user to paste code
            st.info("**A new tab should have opened for Google sign-in.**")
            st.write("**If it did not open automatically,** [click here to authorize]({})".format(st.session_state["auth_url"]))
            code = st.text_input("**Paste the authorization code from the new tab here**:")

            # 4) Once user pastes the code and clicks "Submit Code", we try to fetch_token
            if st.button("Submit Code"):
                if code.strip():
                    try:
                        flow.fetch_token(code=code.strip())
                        creds = flow.credentials
                        st.session_state["google_token"] = pickle.dumps(creds)
                        st.success("Authentication successful! You can proceed.")
                    except Exception as e:
                        st.error(f"Error fetching token: {e}")
                        return None
                else:
                    st.warning("Please paste the code from Google first.")
                # Stop so the user sees success/fail. Next run should have creds
                st.stop()

    if not creds:
        return None

    return build("calendar", "v3", credentials=creds)

def get_or_create_calendar(service, calendar_name, timezone="Asia/Kolkata"):
    if not service:
        return None
    result = service.calendarList().list().execute()
    for cal in result.get("items", []):
        if cal.get("summary") == calendar_name:
            return cal.get("id")
    body = {
        "summary": calendar_name,
        "timeZone": timezone,
    }
    created = service.calendars().insert(body=body).execute()
    return created.get("id")

def get_first_date_on_or_after(start_date, target_weekday):
    """Return the first date on or after start_date that falls on target_weekday (0=Monday)."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

# Example placeholders for your actual usage
theory_mapping = {
    "A1": [("TU", "09:00", "09:50"), ("SA", "12:00", "12:50")],
    "A2": [("TU", "15:00", "15:50"), ("SA", "16:00", "16:50")],
    "B1": [("TU", "10:00", "10:50"), ("WE", "12:00", "12:50")],
    "B2": [("WE", "17:00", "17:50"), ("TH", "14:00", "14:50")],
    "C1": [("TU", "11:00", "11:50"), ("SA", "10:00", "10:50")],
    "C2": [("TU", "17:00", "17:50"), ("FR", "14:00", "14:50")],
    "D1": [("TU", "12:00", "12:50"), ("WE", "09:00", "09:50")],
    "D2": [("WE", "14:00", "14:50"), ("SA", "14:00", "14:50")],
    "E1": [("WE", "11:00", "11:50"), ("SA", "09:00", "09:50")],
    "E2": [("TU", "14:00", "14:50"), ("WE", "15:00", "15:50")],
    "F1": [("WE", "10:00", "10:50"), ("FR", "11:00", "11:50")],
    "F2": [("WE", "16:00", "16:50"), ("TH", "15:00", "15:50")],
    "G1": [("FR", "10:00", "10:50"), ("SA", "11:00", "11:50")],
    "G2": [("TU", "16:00", "16:50"), ("FR", "16:00", "16:50")],
    "TA1": [("TH", "11:00", "11:50")],
    "TA2": [("TH", "17:00", "17:50")],
    "TB1": [("FR", "09:00", "09:50")],
    "TB2": [("FR", "15:00", "15:50")],
    "TC1": [("TH", "09:00", "09:50")],
    "TC2": [("SA", "15:00", "15:50")],
    "TD1": [("TH", "10:00", "10:50")],
    "TD2": [("TH", "16:00", "16:50")],
    "TE1": [("FR", "12:00", "12:50")],
    "TE2": [("FR", "17:00", "17:50")],
    "TF1": [("TH", "08:00", "08:50")],
    "TF2": [("TH", "18:00", "18:50")],
    "TG1": [("WE", "08:00", "08:50")],
    "TG2": [("SA", "18:00", "18:50")],
    "TAA1": [("FR", "10:00", "10:50")],
    "TBB1": [("SA", "11:00", "11:50")],
    "TCC1": [("FR", "08:00", "08:50")],
    "TDD1": [("SA", "08:00", "08:50")],
    "TEE1": [("TU", "08:00", "08:50")],
    "TFF1": [("TH", "12:00", "12:50")],
    "TAA2": [("FR", "16:00", "16:50")],
    "TBB2": [("TU", "16:00", "16:50")],
    "TCC2": [("WE", "18:00", "18:50")],
    "TDD2": [("TU", "18:00", "18:50")],
    "TEE2": [("TH", "18:00", "18:50")],
    "SC2": [("WE", "11:00", "11:50")],
    "CLUBS": [("TH", "12:00", "12:50"), ("SA", "17:00", "17:50"), ("SA", "18:00", "18:50")],
    "ECS": [("TH", "12:00", "12:50"), ("SA", "17:00", "17:50"), ("SA", "18:00", "18:50")],
    "SD2": [("FR", "12:00", "12:50")],
    "SE2": [("SA", "09:00", "09:50")],
    "SE1": [("TU", "14:00", "14:50")],
    "SC1": [("WE", "15:00", "15:50")],
    "SD1": [("FR", "17:00", "17:50")],
    "SF1": [("SA", "17:00", "17:50")]
}

# LAB SLOTS MAPPING (final corrected lab timing table)
lab_mapping = {
    "L1+L2": [("TU", "08:00", "09:40")],
    "L2+L3": [("TU", "09:00", "10:40")],
    "L3+L4": [("TU", "10:00", "11:40")],
    "L4+L5": [("TU", "11:00", "12:40")],
    "L5+L6": [("TU", "12:00", "13:30")],
    "L7+L8": [("WE", "08:00", "09:40")],
    "L8+L9": [("WE", "09:00", "10:40")],
    "L9+L10": [("WE", "10:00", "11:40")],
    "L10+L11": [("WE", "11:00", "12:40")],
    "L11+L12": [("WE", "12:00", "13:30")],
    "L13+L14": [("TH", "08:00", "09:40")],
    "L14+L15": [("TH", "09:00", "10:40")],
    "L15+L16": [("TH", "10:00", "11:40")],
    "L16+L17": [("TH", "11:00", "12:40")],
    "L17+L18": [("TH", "12:00", "13:30")],
    "L19+L20": [("FR", "08:00", "09:40")],
    "L20+L21": [("FR", "09:00", "10:40")],
    "L21+L22": [("FR", "10:00", "11:40")],
    "L22+L23": [("FR", "11:00", "12:40")],
    "L23+L24": [("FR", "12:00", "13:30")],
    "L25+L26": [("SA", "08:00", "09:40")],
    "L26+L27": [("SA", "09:00", "10:40")],
    "L27+L28": [("SA", "10:00", "11:40")],
    "L28+L29": [("SA", "11:00", "12:40")],
    "L29+L30": [("SA", "12:00", "13:30")],
    "L31+L32": [("TU", "14:00", "15:40")],
    "L32+L33": [("TU", "15:00", "16:40")],  # shifted 1 hour
    "L33+L34": [("TU", "16:00", "17:40")],
    "L34+L35": [("TU", "17:00", "18:40")],
    "L35+L36": [("TU", "18:00", "19:30")],  # fixed timing: ends at 7:30 PM
    "L37+L38": [("WE", "14:00", "15:40")],
    "L38+L39": [("WE", "15:00", "16:40")],
    "L39+L40": [("WE", "16:00", "17:40")],
    "L40+L41": [("WE", "17:00", "18:40")],
    "L41+L42": [("WE", "18:00", "19:30")],  # fixed timing: ends at 7:30 PM
    "L43+L44": [("TH", "14:00", "15:40")],
    "L44+L45": [("TH", "15:00", "16:40")],
    "L45+L46": [("TH", "16:00", "17:40")],
    "L46+L47": [("TH", "17:00", "18:40")],  # fixed timing: 5:00 PM - 6:40 PM
    "L47+L48": [("TH", "18:00", "19:30")],  # fixed timing: 6:00 PM - 7:30 PM
    "L49+L50": [("FR", "14:00", "15:40")],
    "L50+L51": [("FR", "15:00", "16:40")],
    "L51+L52": [("FR", "16:00", "17:40")],
    "L52+L53": [("FR", "17:00", "18:40")],
    "L53+L54": [("FR", "18:00", "19:30")],  # fixed timing: ends at 7:30 PM
    "L55+L56": [("SA", "14:00", "15:40")],
    "L56+L57": [("SA", "15:00", "16:40")],
    "L57+L58": [("SA", "16:00", "17:40")],
    "L58+L59": [("SA", "17:00", "18:40")],
    "L59+L60": [("SA", "18:00", "19:30")]   # fixed timing: ends at 7:30 PM
}

weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def create_calendar_events(service, df, semester_start_date, calendar_id,
                           timezone="Asia/Kolkata", notifications=[]):
    """Creates events from a DataFrame with columns: Course, Slot, Venue, Faculty Details."""
    if not service or not calendar_id:
        return False

    overrides = [{"method": "popup", "minutes": m} for m in notifications]
    reminders = {
        "useDefault": False,
        "overrides": overrides
    }

    total_rows = len(df)
    progress_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row["Course"].strip()
        slot_field = row["Slot"].strip()
        venue = row["Venue"].strip()
        faculty = row["Faculty Details"].strip()

        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
            slot_tokens = [tok.strip().upper() for tok in slot_field.split("+") if tok.strip()]
            is_lab = False
            lab_key = None

            # Check if entire slot_field is in lab_mapping
            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                # If any token is in lab_mapping or starts with 'L'
                for tok in slot_tokens:
                    if tok in lab_mapping or tok.startswith("L"):
                        is_lab = True
                        lab_key = tok
                        break

            if is_lab:
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found in mapping. Skipping.")
                    continue
                for day_code, start_str, end_str in mapping:
                    start_hour, start_minute = map(int, start_str.split(":"))
                    end_hour, end_minute = map(int, end_str.split(":"))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                    event = {
                        "summary": summary,
                        "location": venue,
                        "description": faculty,
                        "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                        "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                        "reminders": reminders,
                    }
                    service.events().insert(calendarId=calendar_id, body=event).execute()
            else:
                # Theory
                for tok in slot_tokens:
                    mapping = theory_mapping.get(tok)
                    if not mapping:
                        st.warning(f"Theory slot '{tok}' not found in mapping. Skipping.")
                        continue
                    for day_code, start_str, end_str in mapping:
                        start_hour, start_minute = map(int, start_str.split(":"))
                        end_hour, end_minute = map(int, end_str.split(":"))
                        first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                        dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                        dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                        event = {
                            "summary": summary,
                            "location": venue,
                            "description": faculty,
                            "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                            "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                            "reminders": reminders,
                        }
                        service.events().insert(calendarId=calendar_id, body=event).execute()

        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))

    progress_bar.progress(100)
    return success

def main():
    st.title("Get Notifications on Google Calendar!!!")

    # For demonstration, let's just do the Google auth first
    st.header("Google Authentication")

    service = authenticate_google_calendar_manual()
    if service:
        st.success("You are authenticated! You can now create events.")
        # Example usage
        if st.button("Create Test Event"):
            calendar_id = get_or_create_calendar(service, "Academic Timetable", "Asia/Kolkata")
            if calendar_id:
                # Here you would pass a real DataFrame with "Course", "Slot", "Venue", "Faculty Details"
                # For demonstration, let's pass an empty or minimal DF
                df = pd.DataFrame([{
                    "Course": "Test Course",
                    "Slot": "A1",
                    "Venue": "TestVenue-CB",
                    "Faculty Details": "Test Faculty"
                }])
                semester_start_date = datetime.now().date()
                success = create_calendar_events(
                    service,
                    df,
                    semester_start_date,
                    calendar_id,
                    timezone="Asia/Kolkata",
                    notifications=[10, 5]  # example
                )
                if success:
                    st.success("Test event created!")
    else:
        st.warning("Not authenticated. Please complete the steps above to sign in.")

if __name__ == "__main__":
    main()
